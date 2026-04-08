"""
Chat API Routes

Endpoints for chat functionality and testing.
"""

import json
import logging
import traceback as _traceback
import re
from datetime import date, timedelta
from time import perf_counter as _perf_counter
from pathlib import Path
from fastapi import APIRouter, HTTPException, Depends, Request
from typing import Any, Optional, List
from pydantic import BaseModel, Field
from sqlalchemy import select

from schemas.chat import ChatRequest, ChatResponse
from llm.client import llm_client
from services.chat_service import chat_service
from services.evaluation_metrics_service import evaluation_metrics_service
from services.observability_service import observability_service
from services.backend_trace_service import backend_trace_service
from services.everything_trace_service import everything_trace_service
from services.conversation_audit_service import conversation_audit_service
from services.turn_diagnostics_service import turn_diagnostics_service
from services.response_beautifier_service import response_beautifier_service
from services.db_config_service import db_config_service
from services.config_service import config_service
from services import new_detailed_logger
from config.settings import settings
from core.context_manager import context_manager
from models.database import KBFile, Hotel, get_db
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import OperationalError


router = APIRouter(prefix="/api/chat", tags=["Chat"])
logger = logging.getLogger(__name__)

_DEFAULT_CHAT_TEST_PHASE_PROFILES: dict[str, dict[str, str]] = {
    "pre_booking": {
        "guest_id": "921345",
        "entity_id": "5703",
        "organisation_id": "5703",
        "ticket_source": "whatsapp_bot",
        "flow": "booking_bot",
    },
    "pre_checkin": {
        "guest_id": "921346",
        "entity_id": "5703",
        "organisation_id": "5703",
        "ticket_source": "whatsapp_bot",
    },
    "during_stay": {
        "guest_id": "921348",
        "entity_id": "5703",
        "organisation_id": "5703",
        "ticket_source": "whatsapp_bot",
    },
    "post_checkout": {
        "guest_id": "921347",
        "entity_id": "5703",
        "organisation_id": "5703",
        "ticket_source": "whatsapp_bot",
    },
}


def _normalize_phase_identifier(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not raw:
        return ""
    aliases = {
        "prebooking": "pre_booking",
        "booking": "pre_checkin",
        "precheckin": "pre_checkin",
        "duringstay": "during_stay",
        "instay": "during_stay",
        "in_stay": "during_stay",
        "postcheckout": "post_checkout",
    }
    return aliases.get(raw, raw)


def _normalize_chat_test_profile(profile: Any) -> dict[str, str]:
    if not isinstance(profile, dict):
        return {}

    # Keep only integration-relevant profile fields to avoid leaking arbitrary keys.
    keys = {
        "guest_id",
        "entity_id",
        "organisation_id",
        "room_number",
        "guest_phone",
        "guest_name",
        "group_id",
        "ticket_source",
        "flow",
    }
    normalized: dict[str, str] = {}
    for key in keys:
        value = profile.get(key)
        if value in (None, ""):
            continue
        text = str(value).strip()
        if text:
            normalized[key] = text

    # Accept alternate spellings for organisation/entity IDs.
    if "organisation_id" not in normalized:
        alt = str(profile.get("organization_id") or profile.get("org_id") or "").strip()
        if alt:
            normalized["organisation_id"] = alt
    if "entity_id" not in normalized:
        alt = str(profile.get("organisation_id") or profile.get("organization_id") or profile.get("org_id") or "").strip()
        if alt:
            normalized["entity_id"] = alt
    if "organisation_id" not in normalized and "entity_id" in normalized:
        normalized["organisation_id"] = normalized["entity_id"]

    return normalized


def _load_chat_test_phase_profiles() -> dict[str, dict[str, str]]:
    raw = str(getattr(settings, "chat_test_phase_profiles_json", "") or "").strip()
    parsed: Any = {}
    if raw:
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {}

    defaults: dict[str, dict[str, str]] = {}
    for phase_key, profile_payload in _DEFAULT_CHAT_TEST_PHASE_PROFILES.items():
        phase_id = _normalize_phase_identifier(phase_key)
        if not phase_id:
            continue
        profile = _normalize_chat_test_profile(profile_payload)
        if profile:
            defaults[phase_id] = profile

    overrides: dict[str, dict[str, str]] = {}

    if isinstance(parsed, list):
        for row in parsed:
            if not isinstance(row, dict):
                continue
            phase_id = _normalize_phase_identifier(row.get("phase") or row.get("phase_id"))
            if not phase_id:
                continue
            profile = _normalize_chat_test_profile(row)
            if profile:
                overrides[phase_id] = profile
    elif isinstance(parsed, dict):
        for phase_key, profile_payload in parsed.items():
            phase_id = _normalize_phase_identifier(phase_key)
            if not phase_id:
                continue
            profile = _normalize_chat_test_profile(profile_payload)
            if profile:
                overrides[phase_id] = profile

    merged = dict(defaults)
    merged.update(overrides)
    return merged


def _normalize_service_identifier(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _extract_suggestion_candidates(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    extracted: list[str] = []
    for item in raw:
        text = ""
        if isinstance(item, dict):
            supported = item.get("supported")
            if supported is False:
                continue
            text = str(
                item.get("text")
                or item.get("suggestion")
                or item.get("message")
                or ""
            ).strip()
            evidence = item.get("evidence")
            if isinstance(evidence, list) and not evidence and supported is True:
                # When the model explicitly marks "supported=true", keep; otherwise
                # we still accept short text suggestions as a fallback parse path.
                pass
        else:
            text = str(item or "").strip()
        if text:
            extracted.append(text)
    return extracted


def _sanitize_suggestions(raw: list[str], limit: int = 4) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = " ".join(str(item or "").strip().split())
        if not text:
            continue
        if len(text) > 80:
            continue
        word_count = len(text.split())
        if word_count < 2 or word_count > 12:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
        if len(cleaned) >= max(1, int(limit or 4)):
            break
    return cleaned


def _service_kb_fact_lines(record: dict[str, Any], max_items: int = 16) -> list[str]:
    rows = record.get("facts", []) if isinstance(record, dict) else []
    if not isinstance(rows, list):
        return []
    lines: list[str] = []
    for fact in rows:
        if not isinstance(fact, dict):
            continue
        text = str(fact.get("text") or "").strip()
        if not text:
            continue
        lines.append(text)
        if len(lines) >= max_items:
            break
    return lines


def _display_kb_source_name(value: str) -> str:
    name = str(value or "").strip()
    if not name:
        return "kb_source"
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = re.sub(r"^[0-9a-f]{8}_", "", name, flags=re.IGNORECASE)
    return cleaned or name


async def _load_all_kb_text_from_db(
    *,
    db: AsyncSession,
    hotel_code: str,
    max_chars: int = 50000,
) -> str:
    """Best-effort DB fallback for combined KB text when file sources are stale/empty."""
    code = config_service.resolve_hotel_code(hotel_code)
    hotel_id: Optional[int] = None
    try:
        result = await db.execute(
            select(Hotel.id).where(Hotel.code.ilike(code)).limit(1)
        )
        hotel_id = result.scalar_one_or_none()
        if hotel_id is None:
            return ""

        rows = await db.execute(
            select(KBFile.stored_name, KBFile.content)
            .where(KBFile.hotel_id == hotel_id)
            .order_by(KBFile.id.asc())
        )
        blocks: list[str] = []
        for stored_name, content in rows.all():
            body = str(content or "").strip()
            if not body:
                continue
            source_name = _display_kb_source_name(str(stored_name or "kb_file"))
            blocks.append(f"=== SOURCE: {source_name} (db) ===\n{body}")
        combined = "\n\n".join(blocks).strip()
        if not combined:
            return ""
        if max_chars and max_chars > 0:
            return combined[:max_chars]
        return combined
    except Exception:
        return ""


def _record_evaluation_event(request: ChatRequest, response: ChatResponse, trace_id: str) -> None:
    """Best-effort evaluation event recording."""
    if not bool(getattr(settings, "evaluation_metrics_enabled", True)):
        return
    try:
        evaluation_metrics_service.record_chat_response(
            request=request,
            response=response,
            trace_id=trace_id,
        )
    except Exception as record_error:
        observability_service.log_event(
            "evaluation_metrics_record_failed",
            {
                "trace_id": trace_id,
                "session_id": request.session_id,
                "hotel_code": request.hotel_code,
                "error": str(record_error),
            },
        )


def _trace_chat_event(
    event: str,
    *,
    trace_id: str = "",
    turn_trace_id: str = "",
    http_request: Request | None = None,
    request: ChatRequest | None = None,
    response: ChatResponse | None = None,
    status_code: int | None = None,
    error: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    request_meta = request.metadata if request is not None and isinstance(request.metadata, dict) else {}
    response_meta = response.metadata if response is not None and isinstance(response.metadata, dict) else {}
    endpoint = (
        str(http_request.url.path or "").strip()
        if http_request is not None and getattr(http_request, "url", None) is not None
        else "/api/chat/message"
    )
    method = (
        str(http_request.method or "").strip()
        if http_request is not None
        else "POST"
    )
    payload = {
        "request_message": str(request.message or "").strip() if request is not None else "",
        "request_message_length": len(str(request.message or "")) if request is not None else 0,
        "request_metadata_keys": sorted([str(k) for k in request_meta.keys()])[:80],
        "response_message_length": len(str(response.message or "")) if response is not None else 0,
        "response_state": str(getattr(response.state, "value", response.state) or "") if response is not None else "",
        "response_intent": str(response_meta.get("classified_intent") or "") if response_meta else "",
        "response_source": str(response_meta.get("response_source") or "") if response_meta else "",
        "routing_path": str(response_meta.get("routing_path") or "") if response_meta else "",
        "ticket_created": bool(response_meta.get("ticket_created", False)) if response_meta else False,
    }
    if extra:
        payload.update(extra)
    session_id = str(request.session_id or "").strip() if request is not None else ""
    hotel_code = str(request.hotel_code or "").strip() if request is not None else ""
    channel = (
        str(request.channel or request_meta.get("channel") or "").strip()
        if request is not None
        else ""
    )

    backend_trace_service.log_event(
        event,
        payload,
        trace_id=trace_id,
        turn_trace_id=turn_trace_id,
        session_id=session_id,
        hotel_code=hotel_code,
        channel=channel,
        endpoint=endpoint,
        method=method,
        status_code=status_code,
        component="api.routes.chat.send_message",
        error=error,
    )
    everything_trace_service.log_event(
        event,
        {
            **payload,
            "request_metadata": request_meta,
            "response_metadata": response_meta,
            "request_message_full": str(request.message or "") if request is not None else "",
            "response_message_full": str(response.message or "") if response is not None else "",
        },
        trace_id=trace_id,
        turn_trace_id=turn_trace_id,
        session_id=session_id,
        hotel_code=hotel_code,
        channel=channel,
        endpoint=endpoint,
        method=method,
        status_code=status_code,
        component="api.routes.chat.send_message",
        error=error,
    )
    if turn_trace_id:
        turn_diagnostics_service.log_event(event, payload)


def _enrich_service_llm_display_fields(response: ChatResponse) -> None:
    """Ensure service-label display fields are always present for UI clients."""
    if not isinstance(response.metadata, dict):
        response.metadata = {}
    metadata = response.metadata

    label = str(
        response.service_llm_label
        or metadata.get("service_llm_label")
        or metadata.get("orchestration_target_service_id")
        or ""
    ).strip()
    if not label:
        label = "main"

    confidence_candidates = (
        response.service_llm_confidence,
        metadata.get("service_llm_confidence"),
        metadata.get("classified_confidence"),
        metadata.get("confidence"),
    )
    parsed_confidence: Optional[float] = None
    for candidate in confidence_candidates:
        try:
            value = float(candidate)
        except (TypeError, ValueError):
            continue
        if value != value:  # NaN guard
            continue
        parsed_confidence = max(0.0, min(1.0, value))
        break

    response.service_llm_label = label
    response.service_llm_confidence = parsed_confidence
    metadata["service_llm_label"] = label
    if parsed_confidence is not None:
        metadata["service_llm_confidence"] = parsed_confidence


async def _attach_display_message(response: ChatResponse) -> None:
    """Create display-only beautified message without mutating canonical message."""
    if not isinstance(response.metadata, dict):
        response.metadata = {}
    canonical = str(response.message or "").strip()
    display_message, beautifier_meta = await response_beautifier_service.beautify_display_text(
        canonical,
        state=str(getattr(response.state, "value", response.state) or ""),
        metadata=response.metadata,
    )
    response.display_message = str(display_message or canonical).strip()
    response.metadata["display_message"] = response.display_message
    response.metadata["canonical_message"] = canonical
    if isinstance(beautifier_meta, dict):
        response.metadata.update(beautifier_meta)


async def _inject_form_trigger(response: ChatResponse, *, user_message: str = "") -> None:
    """
    When the bot is actively collecting info for a ticketing-enabled service,
    extract that service's form fields and attach them to the response metadata
    so the frontend renders an inline form instead of plain text Q&A.
    """
    import sys as _sys
    from services.form_fields_service import extract_form_fields
    from services.config_service import config_service
    from services.form_mode_service import (
        canonicalize_trigger_pending_data,
        infer_trigger_value_from_message,
        normalize_form_field_key,
        normalize_trigger_missing_fields,
        resolve_trigger_field_value,
        strip_form_confirmation_instructions,
    )

    if not isinstance(response.metadata, dict):
        response.metadata = {}
    meta = response.metadata
    if not isinstance(meta.get("entities"), dict):
        meta["entities"] = {}
    entities_meta = meta["entities"]
    state_val = str(getattr(response.state, "value", response.state) or "").strip().lower()
    orchestration_action = str(
        entities_meta.get("orchestration_action")
        or meta.get("orchestration_action")
        or ""
    ).strip().lower()
    missing_fields_raw = (
        entities_meta.get("missing_fields")
        or meta.get("missing_fields")
        or []
    )
    if isinstance(missing_fields_raw, (list, tuple)):
        missing_fields = [str(item).strip() for item in missing_fields_raw if str(item).strip()]
    elif str(missing_fields_raw or "").strip():
        missing_fields = [str(missing_fields_raw).strip()]
    else:
        missing_fields = []
    pending_action_hint = str(
        entities_meta.get("pending_action")
        or meta.get("pending_action")
        or ""
    ).strip().lower()
    response_text = str(response.message or "").strip().lower()
    display_text = str(
        getattr(response, "display_message", "")
        or meta.get("display_message")
        or ""
    ).strip().lower()
    message_collection_hint = any(
        phrase in response_text or phrase in display_text
        for phrase in (
            "please fill in the details below",
            "please fill in the booking details below",
            "please fill in the form below",
            "please proceed with the booking details",
        )
    )
    collecting_signal = (
        state_val == "awaiting_info"
        or orchestration_action == "collect_info"
        or bool(missing_fields)
        or pending_action_hint.startswith("collect_")
        or message_collection_hint
    )
    print(
        (
            f"[form_trigger] state={state_val!r} "
            f"orchestration_action={orchestration_action!r} "
            f"missing_fields={missing_fields!r} "
            f"pending_action_hint={pending_action_hint!r} "
            f"message_collection_hint={message_collection_hint} "
            f"collecting_signal={collecting_signal}"
        ),
        file=_sys.stderr,
        flush=True,
    )

    if meta.get("form_trigger") or meta.get("ticket_created"):
        print(f"[form_trigger] SKIP: already has form_trigger or ticket_created", file=_sys.stderr, flush=True)
        return

    # â”€â”€ Resolve target_service_id from ALL available sources â”€â”€
    _norm_svc = lambda s: str(s or "").strip().lower().replace(" ", "_").replace("-", "_")
    target_service_id = ""
    # Source 1: entities.target_service_id (always populated by chat_service)
    if not target_service_id:
        target_service_id = str(entities_meta.get("target_service_id") or "").strip()
    # Source 2: top-level metadata
    if not target_service_id:
        target_service_id = str(
            meta.get("orchestration_target_service_id")
            or meta.get("pending_service_id")
            or ""
        ).strip()
    # Source 3: service_llm_label (display name fallback)
    if not target_service_id:
        label = str(
            meta.get("service_llm_label")
            or getattr(response, "service_llm_label", "")
            or ""
        ).strip()
        if label and label.lower() != "main":
            target_service_id = label

    print(f"[form_trigger] target_service_id={target_service_id!r}", file=_sys.stderr, flush=True)

    if not target_service_id:
        print(f"[form_trigger] SKIP: no target_service_id found", file=_sys.stderr, flush=True)
        return

    # â”€â”€ Load service definition (JSON first, then DB fallback) â”€â”€
    service = config_service.get_service(target_service_id)
    if not service:
        # Try normalized version (spaces â†’ underscores)
        service = config_service.get_service(_norm_svc(target_service_id))
    # DB fallback: service may exist only in the database (admin-created)
    if not service:
        try:
            from services.db_config_service import db_config_service as _dcs
            _db_svcs = await _dcs.get_services()
            _target_norm = _norm_svc(target_service_id)
            for _candidate in (_db_svcs or []):
                if not isinstance(_candidate, dict):
                    continue
                if _norm_svc(_candidate.get("id")) == _target_norm or _norm_svc(_candidate.get("name")) == _target_norm:
                    service = dict(_candidate)
                    break
        except Exception as _db_exc:
            print(f"[form_trigger] DB fallback error: {_db_exc!r}", file=_sys.stderr, flush=True)
    ticketing_enabled = bool(service.get("ticketing_enabled", False)) if service else False
    print(f"[form_trigger] service={service.get('id') if service else None}, ticketing_enabled={ticketing_enabled}", file=_sys.stderr, flush=True)

    if not service or not ticketing_enabled:
        print(f"[form_trigger] SKIP: service not found or ticketing not enabled", file=_sys.stderr, flush=True)
        return

    # Respect ticketing_mode: only inject form UI when mode is "form".
    # "text" mode keeps the conversational text collection (no form injected).
    # "none" means ticketing is disabled (already caught above via ticketing_enabled).
    ticketing_mode = str(service.get("ticketing_mode") or "").strip().lower()
    if ticketing_mode == "text":
        print(f"[form_trigger] SKIP: ticketing_mode=text, using conversational collection", file=_sys.stderr, flush=True)
        return
    if ticketing_mode == "none":
        print(f"[form_trigger] SKIP: ticketing_mode=none", file=_sys.stderr, flush=True)
        return
    # If ticketing_mode is empty/unset, default to form injection for backward compat
    if ticketing_mode and ticketing_mode not in ("form", "text", "none"):
        ticketing_mode = "form"

    # â”€â”€ Trigger-field gate: do not show form until the trigger field is collected â”€â”€
    # The service's form_config.trigger_field specifies which field (e.g. room_type,
    # terminal) must be known before the form is shown.
    # We check TWO signals:
    #   1. trigger field is explicitly listed in missing_fields â†’ definitely not collected
    #   2. trigger field value is absent from pending_data â†’ not collected yet
    # The form only shows when the trigger field is NOT in missing_fields AND its
    # value IS present in pending_data.
    svc_form_config = service.get("form_config") if isinstance(service.get("form_config"), dict) else {}
    trigger_field_cfg = svc_form_config.get("trigger_field") if isinstance(svc_form_config.get("trigger_field"), dict) else {}
    trigger_field_id = normalize_form_field_key(trigger_field_cfg.get("id"))
    trigger_collected = False
    if trigger_field_id:
        trigger_field_label = str(trigger_field_cfg.get("label") or trigger_field_id).strip()
        collected_data = (
            entities_meta.get("pending_data")
            or meta.get("pending_data")
            or {}
        )
        collected_data, _trigger_alias_key, _ = canonicalize_trigger_pending_data(
            collected_data,
            trigger_field_id,
            trigger_field_label,
        )
        entities_meta["pending_data"] = dict(collected_data)
        meta["pending_data"] = dict(collected_data)
        _, trigger_value_raw = resolve_trigger_field_value(
            collected_data,
            trigger_field_id,
            trigger_field_label,
        )
        trigger_value = str(trigger_value_raw or "").strip()
        if not trigger_value:
            inferred_key, inferred_value = infer_trigger_value_from_message(
                collected_data,
                trigger_field_id,
                trigger_field_label,
                user_message,
            )
            if not inferred_value:
                inferred_key, inferred_value = infer_trigger_value_from_message(
                    collected_data,
                    trigger_field_id,
                    trigger_field_label,
                    response.message,
                )
            if inferred_value:
                infer_field = normalize_form_field_key(inferred_key) or trigger_field_id
                collected_data[infer_field] = inferred_value
                entities_meta["pending_data"] = dict(collected_data)
                meta["pending_data"] = dict(collected_data)
                trigger_value = str(inferred_value).strip()
                meta["form_trigger_inferred_trigger_value"] = True
                print(
                    f"[form_trigger] inferred trigger_field '{trigger_field_id}' from message: {trigger_value!r}",
                    file=_sys.stderr,
                    flush=True,
                )
        missing_fields = normalize_trigger_missing_fields(
            missing_fields,
            trigger_field_id,
            trigger_field_label,
            trigger_value_present=bool(trigger_value),
        )
        entities_meta["missing_fields"] = list(missing_fields)
        meta["missing_fields"] = list(missing_fields)
        missing_lower = [normalize_form_field_key(f) for f in missing_fields]
        trigger_missing = trigger_field_id in missing_lower
        trigger_collected = bool(trigger_value) and not trigger_missing
        if trigger_collected:
            if orchestration_action == "respond_only":
                orchestration_action = "collect_info"
                entities_meta["orchestration_action"] = orchestration_action
                meta["orchestration_action"] = orchestration_action
                meta["form_mode_forced_collect_info"] = True
            if not str(entities_meta.get("pending_action") or "").strip():
                entities_meta["pending_action"] = "collect_form_details"
            if not str(meta.get("pending_action") or "").strip():
                meta["pending_action"] = "collect_form_details"
            print(
                f"[form_trigger] trigger_field '{trigger_field_id}' collected: {trigger_value!r}",
                file=_sys.stderr,
                flush=True,
            )
        elif trigger_missing:
            print(
                f"[form_trigger] SKIP: trigger_field '{trigger_field_id}' still in missing_fields",
                file=_sys.stderr, flush=True,
            )
            return
        else:
            if not collecting_signal:
                print(
                    f"[form_trigger] SKIP: no collection signal and trigger_field '{trigger_field_id}' missing",
                    file=_sys.stderr,
                    flush=True,
                )
                return
            print(
                f"[form_trigger] SKIP: trigger_field '{trigger_field_id}' not yet in pending_data "
                f"(available keys: {list(collected_data.keys())})",
                file=_sys.stderr, flush=True,
            )
            return
    else:
        if not collecting_signal:
            print(f"[form_trigger] SKIP: no collection signal", file=_sys.stderr, flush=True)
            return
        if orchestration_action == "respond_only" and not message_collection_hint:
            print(
                f"[form_trigger] SKIP: orchestration_action=respond_only (no form hint, no trigger field)",
                file=_sys.stderr,
                flush=True,
            )
            return

    fields = await extract_form_fields(service)
    print(f"[form_trigger] extracted {len(fields)} field(s): {[f['id'] for f in fields]}", file=_sys.stderr, flush=True)

    if not fields:
        print(f"[form_trigger] SKIP: no fields extracted", file=_sys.stderr, flush=True)
        return

    response.metadata["form_trigger"] = True
    response.metadata["form_fields"] = fields
    response.metadata["form_service_id"] = target_service_id

    # Strip only form-field listings from the bot message (the form UI handles those).
    # Keep room/service descriptions, features, and other informational content intact.
    import re as _re

    # Build a set of field-like keywords so we only strip lines that name form fields,
    # not lines that describe room features or amenities.
    _field_keywords: set[str] = set()
    for _f in fields:
        _field_keywords.add(str(_f.get("id") or "").strip().lower().replace("_", " "))
        _field_keywords.add(str(_f.get("label") or "").strip().lower())
    _field_keywords.discard("")

    def _line_is_field_listing(line_text: str) -> bool:
        """Return True if a bullet/numbered line looks like a form field label."""
        # Extract the text after the bullet/number marker
        inner = _re.sub(r"^[-*\u2022]\s+|^\d+[.)]\s+", "", line_text.strip())
        inner = _re.sub(r"\*{1,2}", "", inner).strip().rstrip(":").strip().lower()
        if not inner:
            return True  # empty bullet â†’ safe to remove
        # Check if this line matches a known form field label/id
        for kw in _field_keywords:
            if kw in inner or inner in kw:
                return True
        # Generic "provide your X" patterns are field prompts
        if _re.match(r"^(your\s+|the\s+|guest.?s?\s+)?(full\s+)?name", inner):
            return True
        if _re.match(r"^(phone|contact|email|check.?in|check.?out|date|time|guests?|special)", inner):
            return True
        return False

    def _strip_field_list(msg: str) -> str:
        if not msg:
            return "Please fill in the details below."
        # Remove only bullet/numbered lines that look like form field labels
        cleaned_lines = []
        for line in msg.split("\n"):
            stripped = line.strip()
            is_bullet = bool(_re.match(r"^[-*\u2022]\s+", stripped) or _re.match(r"^\d+[.)]\s+", stripped))
            if is_bullet and _line_is_field_listing(stripped):
                continue
            cleaned_lines.append(line)
        cleaned = "\n".join(cleaned_lines).strip()
        # Remove trailing prompts like "could you please provide the following details:"
        cleaned = _re.sub(
            r"[,.]?\s*(?:could you |can you |please |kindly )*(?:provide|share|give|send)\s+(?:us\s+|me\s+)?(?:the\s+)?(?:following\s+)?(?:details|information|info)\s*(?:to proceed[^?:.]*)?[?:.]?\s*$",
            ".",
            cleaned,
            flags=_re.IGNORECASE,
        ).strip()
        # Remove dangling clauses that truly end with a colon and nothing after
        # (e.g. "and includes:" at end of text). Do NOT strip mid-sentence "offers:" etc.
        cleaned = _re.sub(
            r"(?:\s*(?:,\s*)?(?:and|which|that|,)\s+)?(?:offers|includes|featuring|features|comes with|has|provides)\s*:\s*$",
            ".",
            cleaned,
            flags=_re.IGNORECASE,
        ).strip()
        # Remove LLM-generated "Please fill in the details below" since we append our own
        cleaned = _re.sub(
            r"\.?\s*Please fill in the (?:details|form|booking details) below\.?\s*",
            ". ",
            cleaned,
            flags=_re.IGNORECASE,
        ).strip()
        # Fix double periods, ". ." artifacts, or space-period patterns
        cleaned = _re.sub(r"\.\s*\.", ".", cleaned)
        cleaned = _re.sub(r"\s+\.", ".", cleaned)
        # Clean up trailing colons or empty sentences
        cleaned = _re.sub(r"[:\s]+$", ".", cleaned).strip()
        if cleaned and cleaned != msg.strip():
            cleaned = cleaned.rstrip(".") + ". Please fill in the details below."
        return cleaned or "Please fill in the details below."

    response.message = _strip_field_list(strip_form_confirmation_instructions(response.message))
    if response.display_message:
        response.display_message = _strip_field_list(
            strip_form_confirmation_instructions(response.display_message)
        )
    # Also clean metadata.display_message since frontend reads it first
    if isinstance(response.metadata, dict) and response.metadata.get("display_message"):
        response.metadata["display_message"] = _strip_field_list(
            strip_form_confirmation_instructions(str(response.metadata["display_message"]))
        )

    print(f"[form_trigger] SUCCESS: form_trigger set for service={target_service_id!r}", file=_sys.stderr, flush=True)


class FormSubmitRequest(BaseModel):
    """Payload for direct form-based ticket creation."""
    session_id: str
    hotel_code: str = "DEFAULT"
    service_id: str
    form_data: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FormValidateRequest(BaseModel):
    """Payload for LLM-based form field validation."""
    service_id: str
    form_fields: list[dict[str, Any]] = Field(default_factory=list)
    form_data: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


_FORM_VALIDATE_SYSTEM_PROMPT = """\
You are a smart form-field validator for a hotel service request.
You will receive a list of form fields (with id, label, type) and the values the guest entered.

Your job: validate each field using your knowledge of real-world formats. Be helpful â€” catch genuinely wrong data but accept anything plausible.

CRITICAL: Match the validation rule to each field based ONLY on its "type" attribute (date, time, text, tel, textarea, etc.). Do NOT infer validation from the field's label or id. For example, a field with type="text" must be validated as text even if its label contains the word "date" or "time".

Rules per field type:

- **Name fields** (full name, guest name): Accept any text that could be a name, including single names (e.g., "Sana", "Raj"), hyphenated names, names with apostrophes (e.g., "O'Brien"), or names with accents. Reject ONLY if it contains numbers, is a single character, or is obviously not a name (e.g., "asdf", "xxx").

- **Phone / tel fields**: The value includes a country code prefix (e.g., +91, +1, +44). You MUST strictly validate phone numbers using your knowledge of each country's telecom rules. This is NOT a lenient field â€” phone validation must be accurate.
  * India (+91): EXACTLY 10 digits after +91. First digit MUST be 6, 7, 8, or 9. Numbers starting with 0, 1, 2, 3, 4, or 5 are INVALID in India. Example: +911234567890 â†’ REJECT (starts with 1). +919876543210 â†’ ACCEPT (starts with 9).
  * US/Canada (+1): EXACTLY 10 digits. First digit of area code cannot be 0 or 1.
  * UK (+44): 10-11 digits after +44.
  * UAE (+971): EXACTLY 9 digits after +971.
  * For other countries, use your knowledge of their phone number format.
  * Reject obviously fake/placeholder numbers: all-same digits (9999999999, 1111111111), sequential (1234567890, 9876543210), all-zeros, or any pattern no real person would have. A real phone number should look random/natural.
  * Think like a human â€” would a hotel receptionist accept this number? If it looks fake, reject it politely.

- **Email fields**: Must have @ and a dot after @. Accept most formats.
- **Date fields** (type=date): Must be a valid date. Reject past dates. If check-in and check-out both exist, check-out must be after check-in.
- **Time fields** (type=time): Must be a valid time in HH:MM or HH:MM:SS format (24-hour or 12-hour with AM/PM). Accept any reasonable time value. Do NOT apply date validation rules to time fields.
- **Number fields**: Must be a positive whole number. Reject only values above 100.
- **Textarea / description fields**: Must have at least 2 real words. Reject only single characters or random key-mashing.
- **Other text fields**: Accept any reasonable non-empty text.

For fields OTHER than phone, be lenient â€” when in doubt, accept. For phone numbers, be STRICT and follow the country rules above exactly.

Return ONLY a valid JSON object:
{"valid": true, "errors": []}
or
{"valid": false, "errors": [{"field_id": "...", "message": "..."}]}

Error messages must be short and helpful. Do NOT wrap in markdown. Return raw JSON only.\
"""


@router.post("/form-validate")
async def validate_form(request: FormValidateRequest) -> dict[str, Any]:
    """
    Validation is intentionally bypassed for form submissions.
    Always allow submit and accept user-entered values as-is.
    """
    _ = request
    return {"valid": True, "errors": []}


_FORM_CONFIRM_SYSTEM_PROMPT = """\
You are a friendly hotel assistant. A guest just submitted a service request and a ticket has been created.

Generate a SHORT, warm confirmation message (2-3 sentences max). Include:
1. Acknowledge what was requested (use the details provided)
2. A service-appropriate next-step (e.g., "our team will contact you", "your order will be delivered", "we'll arrange the pickup")
3. The ticket ID for reference

Rules:
- Do NOT ask any questions
- Do NOT use markdown formatting (no bold, no bullets, no headers)
- Do NOT list the fields back as a structured list â€” weave them naturally into the message
- Keep it conversational and warm
- Do NOT say "thank you for providing" or similar filler\
"""


async def _generate_confirmation_message(
    service_name: str,
    form_data: dict[str, Any],
    ticket_id: str,
) -> str:
    """Generate an LLM confirmation message after ticket creation."""
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        model = str(getattr(settings, "openai_model", None) or "gpt-4o-mini")

        details = "; ".join(
            f"{k.replace('_', ' ').title()}: {v}"
            for k, v in form_data.items()
            if str(v).strip()
        )

        user_prompt = (
            f"Service: {service_name}\n"
            f"Ticket ID: {ticket_id}\n"
            f"Guest provided details: {details}"
        )

        completion = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _FORM_CONFIRM_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.7,
            max_tokens=200,
        )

        msg = str(completion.choices[0].message.content or "").strip()
        if msg:
            return msg
    except Exception as exc:
        logger.warning("form-confirm: LLM message generation failed: %s", exc)

    # Fallback to static message
    return (
        f"Your {service_name} request has been submitted successfully."
        f" Ticket ID: {ticket_id or 'assigned'}."
    )


def _first_non_empty_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _normalize_phone_text(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return ""
    if raw.startswith("+"):
        return f"+{digits}"
    return digits


def _parse_iso_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


_STAY_CHECKIN_KEYS: tuple[str, ...] = (
    "stay_checkin_date",
    "check_in",
    "checkin",
    "check_in_date",
    "checkin_date",
    "booking_check_in_date",
)
_STAY_CHECKOUT_KEYS: tuple[str, ...] = (
    "stay_checkout_date",
    "check_out",
    "checkout",
    "check_out_date",
    "checkout_date",
    "booking_check_out_date",
)
_SERVICE_DATE_FIELD_HINT = re.compile(
    r"(date|check[ _-]?in|check[ _-]?out|arrival|departure|event|appointment|pickup|dropoff|drop)",
    re.IGNORECASE,
)
_SERVICE_DATE_EXCLUDED_KEYS: set[str] = {
    *_STAY_CHECKIN_KEYS,
    *_STAY_CHECKOUT_KEYS,
    "created_at",
    "updated_at",
    "booking_id",
}
_VALIDATION_CRITICAL_FIELD_HINT = re.compile(
    r"(phone|mobile|contact|email|date|check[ _-]?in|check[ _-]?out|arrival|departure|event)",
    re.IGNORECASE,
)


def _parse_flexible_date(value: Any) -> date | None:
    """Parse YYYY-MM-DD dates, including datetime-like strings containing a date token."""
    direct = _parse_iso_date(value)
    if direct is not None:
        return direct

    text = str(value or "").strip()
    if not text:
        return None

    # Accept common human-entered formats like DD-MM-YYYY / DD/MM/YYYY.
    dmy_match = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b", text)
    if dmy_match:
        try:
            day = int(dmy_match.group(1))
            month = int(dmy_match.group(2))
            year = int(dmy_match.group(3))
            return date(year, month, day)
        except ValueError:
            return None

    token_match = re.search(r"\b\d{4}-\d{2}-\d{2}\b", text)
    if not token_match:
        return None
    return _parse_iso_date(token_match.group(0))


def _pick_first_parsed_date(*sources: Any, keys: tuple[str, ...]) -> date | None:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in keys:
            parsed = _parse_flexible_date(source.get(key))
            if parsed is not None:
                return parsed
    return None


def _looks_like_service_date_field(field_id: str, label: str = "", field_type: str = "") -> bool:
    ftype = str(field_type or "").strip().lower()
    if ftype in {"date", "datetime", "datetime-local"}:
        return True
    # If the field has an explicit non-date type, trust it â€” do NOT override
    # with keyword heuristics. This prevents time, text, and other fields from
    # being misclassified as date fields just because their id/label contains
    # keywords like "arrival", "departure", "event", etc.
    if ftype and ftype not in {"date", "datetime", "datetime-local"}:
        return False
    joined = f"{field_id} {label}".strip()
    if not joined:
        return False
    return bool(_SERVICE_DATE_FIELD_HINT.search(joined))


def _collect_service_date_field_ids(
    *,
    form_fields: list[dict[str, Any]],
    merged_form_data: dict[str, Any],
) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()

    def _add(field_id: str) -> None:
        normalized = str(field_id or "").strip()
        if not normalized:
            return
        if normalized.startswith("_"):
            return
        if normalized.lower() in _SERVICE_DATE_EXCLUDED_KEYS:
            return
        if normalized in seen:
            return
        seen.add(normalized)
        ids.append(normalized)

    for field in form_fields or []:
        if not isinstance(field, dict):
            continue
        field_id = str(field.get("id") or "").strip()
        if not field_id:
            continue
        if _looks_like_service_date_field(
            field_id=field_id,
            label=str(field.get("label") or ""),
            field_type=str(field.get("type") or ""),
        ):
            _add(field_id)

    # Build a lookup of field_id -> field_type from form_fields so the
    # keyword heuristic below can respect explicit non-date types.
    _field_type_by_id: dict[str, str] = {}
    _declared_field_ids: set[str] = set()
    for field in form_fields or []:
        if isinstance(field, dict):
            _fid = str(field.get("id") or "").strip()
            _ftype = str(field.get("type") or "").strip()
            if _fid:
                _declared_field_ids.add(_fid)
            if _fid and _ftype:
                _field_type_by_id[_fid] = _ftype

    for key, value in (merged_form_data or {}).items():
        field_id = str(key or "").strip()
        if not field_id:
            continue
        if field_id.startswith("_"):
            continue
        if field_id.lower() in _SERVICE_DATE_EXCLUDED_KEYS:
            continue
        if not str(value or "").strip():
            continue
        # When a form schema exists, validate only declared field IDs.
        # This avoids false positives from alias/mismatch keys in pending data
        # (for example "arrival_time" when the configured field is "arrival"
        # with type=time).
        if _declared_field_ids and field_id not in _declared_field_ids:
            continue
        if not _looks_like_service_date_field(
            field_id=field_id,
            field_type=_field_type_by_id.get(field_id, ""),
        ):
            continue
        _add(field_id)

    return ids

def _validate_service_date_boundaries(
    *,
    phase_id: str,
    form_fields: list[dict[str, Any]],
    merged_form_data: dict[str, Any],
    req_meta: dict[str, Any],
    context: Any = None,
) -> list[dict[str, str]]:
    phase = _normalize_phase_identifier(phase_id)
    if phase not in {"pre_checkin", "during_stay", "post_checkout"}:
        return []

    safe_form_data = merged_form_data if isinstance(merged_form_data, dict) else {}
    safe_meta = req_meta if isinstance(req_meta, dict) else {}
    pending = getattr(context, "pending_data", None) if context is not None else None
    safe_pending = pending if isinstance(pending, dict) else {}

    booking_context: dict[str, Any] = {}
    if context is not None:
        booking_context = {
            "booking_check_in_date": getattr(context, "booking_check_in_date", None),
            "booking_check_out_date": getattr(context, "booking_check_out_date", None),
        }

    stay_check_in = _pick_first_parsed_date(
        safe_meta,
        safe_form_data,
        safe_pending,
        booking_context,
        keys=_STAY_CHECKIN_KEYS,
    )
    stay_check_out = _pick_first_parsed_date(
        safe_meta,
        safe_form_data,
        safe_pending,
        booking_context,
        keys=_STAY_CHECKOUT_KEYS,
    )

    date_field_ids = _collect_service_date_field_ids(
        form_fields=form_fields,
        merged_form_data=safe_form_data,
    )
    if not date_field_ids:
        return []

    # Fail closed: for in-stay/post-checkout phases we must know booking dates
    # before accepting any service date.
    if stay_check_in is None or stay_check_out is None:
        first_id = date_field_ids[0]
        return [{
            "field_id": first_id,
            "message": "We could not verify your stay dates. Please refresh and try again.",
        }]

    if stay_check_out < stay_check_in:
        stay_check_in, stay_check_out = stay_check_out, stay_check_in

    field_label_by_id: dict[str, str] = {}
    for field in form_fields or []:
        if not isinstance(field, dict):
            continue
        field_id = str(field.get("id") or "").strip()
        if not field_id:
            continue
        label = str(field.get("label") or "").strip()
        if label:
            field_label_by_id[field_id] = label

    errors: list[dict[str, str]] = []
    for field_id in date_field_ids:
        raw_value = safe_form_data.get(field_id)
        text_value = str(raw_value or "").strip()
        if not text_value:
            continue

        parsed_value = _parse_flexible_date(raw_value)
        if parsed_value is None:
            label = field_label_by_id.get(field_id) or field_id.replace("_", " ").title()
            errors.append({
                "field_id": field_id,
                "message": f"Please enter a valid date for {label} (YYYY-MM-DD).",
            })
            continue

        if phase in {"pre_checkin", "during_stay"}:
            if parsed_value < stay_check_in or parsed_value > stay_check_out:
                errors.append({
                    "field_id": field_id,
                    "message": (
                        f"Please select a date within your stay "
                        f"({stay_check_in.isoformat()} to {stay_check_out.isoformat()})."
                    ),
                })
        elif phase == "post_checkout" and parsed_value < stay_check_out:
            errors.append({
                "field_id": field_id,
                "message": (
                    "Please select a date on or after your check-out date "
                    f"({stay_check_out.isoformat()})."
                ),
            })

    return errors


def _build_fail_closed_validation_errors(
    *,
    form_fields: list[dict[str, Any]],
    form_data: dict[str, Any],
) -> list[dict[str, str]]:
    safe_fields = form_fields if isinstance(form_fields, list) else []
    safe_data = form_data if isinstance(form_data, dict) else {}
    critical_ids: list[str] = []
    seen: set[str] = set()

    for field in safe_fields:
        if not isinstance(field, dict):
            continue
        field_id = str(field.get("id") or "").strip()
        if not field_id:
            continue
        if field_id in seen:
            continue
        field_type = str(field.get("type") or "").strip().lower()
        label = str(field.get("label") or "").strip()
        required = bool(field.get("required"))
        has_value = bool(str(safe_data.get(field_id) or "").strip())
        looks_critical = (
            field_type in {"date", "datetime", "datetime-local", "tel", "phone", "email", "number"}
            or bool(_VALIDATION_CRITICAL_FIELD_HINT.search(f"{field_id} {label}".strip()))
            or required
        )
        if looks_critical and has_value:
            seen.add(field_id)
            critical_ids.append(field_id)

    if not critical_ids:
        fallback = ""
        for field in safe_fields:
            if not isinstance(field, dict):
                continue
            candidate = str(field.get("id") or "").strip()
            if candidate:
                fallback = candidate
                break
        if fallback:
            critical_ids.append(fallback)

    return [
        {
            "field_id": field_id,
            "message": "Validation is temporarily unavailable. Please try again.",
        }
        for field_id in critical_ids
    ]


def _coerce_positive_int(value: Any, *, default: int = 1, maximum: int = 20) -> int:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return min(parsed, maximum)


def _extract_prebooking_booking_payload(
    *,
    service_name: str,
    form_data: dict[str, Any],
    req_meta: dict[str, Any],
    context: Any,
) -> dict[str, Any]:
    form = form_data if isinstance(form_data, dict) else {}
    meta = req_meta if isinstance(req_meta, dict) else {}

    phase_hint = _normalize_phase_identifier(
        _first_non_empty_text(
            meta.get("phase"),
            meta.get("phase_id"),
            meta.get("booking_phase"),
        )
    )
    flow_hint = str(meta.get("flow") or "").strip().lower()
    if phase_hint and phase_hint != "pre_booking":
        return {}
    if not phase_hint and flow_hint not in {"booking", "booking_bot", "engage"}:
        return {}

    def _pick(*keys: str) -> str:
        for key in keys:
            if key in form and str(form.get(key) or "").strip():
                return str(form.get(key) or "").strip()
        for key in keys:
            if key in meta and str(meta.get(key) or "").strip():
                return str(meta.get(key) or "").strip()
        return ""

    guest_phone = _normalize_phone_text(
        _first_non_empty_text(
            _pick(
                "guest_phone",
                "phone",
                "phone_number",
                "contact_number",
                "mobile",
                "booking_guest_phone",
                "wa_number",
                "waNumber",
            ),
            getattr(context, "guest_phone", None),
        )
    )
    if not guest_phone:
        user_id_as_phone = _normalize_phone_text(meta.get("user_id"))
        if len("".join(ch for ch in user_id_as_phone if ch.isdigit())) >= 10:
            guest_phone = user_id_as_phone
    if not guest_phone:
        return {}

    check_in = _parse_iso_date(
        _pick(
            "check_in",
            "checkin",
            "check_in_date",
            "checkin_date",
            "arrival_date",
            "booking_check_in_date",
            "stay_checkin_date",
        )
    )
    check_out = _parse_iso_date(
        _pick(
            "check_out",
            "checkout",
            "check_out_date",
            "checkout_date",
            "departure_date",
            "booking_check_out_date",
            "stay_checkout_date",
        )
    )
    if check_in and not check_out:
        check_out = check_in + timedelta(days=1)
    elif check_out and not check_in:
        check_in = check_out - timedelta(days=1)
    elif check_in and check_out and check_out <= check_in:
        check_out = check_in + timedelta(days=1)
    if check_in is None or check_out is None:
        return {}

    guest_name = _first_non_empty_text(
        _pick("guest_name", "full_name", "name", "user_name", "booking_guest_name"),
        getattr(context, "guest_name", None),
    )
    property_name = _first_non_empty_text(
        _pick(
            "property_name",
            "property",
            "hotel_name",
            "hotel",
            "destination",
            "booking_property_name",
        ),
        getattr(context, "booking_property_name", None),
    )
    # Fallback: use property scope set during multi-property selection
    if not property_name:
        property_name = _first_non_empty_text(
            meta.get("_selected_property_scope_name"),
            form.get("_selected_property_scope_name"),
        )
    if not property_name:
        _active_names = meta.get("_active_property_names") or form.get("_active_property_names")
        if isinstance(_active_names, list) and len(_active_names) == 1:
            property_name = str(_active_names[0]).strip()
    room_number = _pick("room_number", "room_no", "booking_room_number")
    room_type = _pick("room_type", "room_category", "booking_room_type")
    guests_count = _coerce_positive_int(
        _pick("num_guests", "guests", "guest_count", "party_size", "adults"),
        default=1,
        maximum=20,
    )
    status = str(_pick("status", "booking_status") or "reserved").strip().lower()
    if status not in {"reserved", "confirmed", "checked_in", "checked_out", "cancelled"}:
        status = "reserved"
    special_requests = _pick("special_requests", "notes", "requirements", "request_notes")
    source_channel = _first_non_empty_text(meta.get("channel"), getattr(context, "channel", None), "web")

    return {
        "guest_phone": guest_phone,
        "guest_name": guest_name or None,
        "property_name": property_name or None,
        "room_number": room_number or None,
        "room_type": room_type or None,
        "check_in_date": check_in,
        "check_out_date": check_out,
        "num_guests": guests_count,
        "status": status,
        "source_channel": source_channel,
        "special_requests": special_requests or None,
        "derived_phase_hint": phase_hint or "pre_booking",
        "service_name": str(service_name or "").strip(),
    }


async def _persist_prebooking_booking_if_possible(
    *,
    hotel_code: str,
    service_name: str,
    form_data: dict[str, Any],
    req_meta: dict[str, Any],
    context: Any,
    db_session: AsyncSession,
) -> dict[str, Any]:
    payload = _extract_prebooking_booking_payload(
        service_name=service_name,
        form_data=form_data,
        req_meta=req_meta,
        context=context,
    )
    if not payload:
        return {}

    try:
        from services.booking_service import create_booking

        created = await create_booking(
            hotel_code=hotel_code,
            guest_phone=str(payload.get("guest_phone") or "").strip(),
            guest_name=str(payload.get("guest_name") or "").strip() or None,
            property_name=str(payload.get("property_name") or "").strip() or None,
            room_number=str(payload.get("room_number") or "").strip() or None,
            room_type=str(payload.get("room_type") or "").strip() or None,
            check_in_date=payload.get("check_in_date"),
            check_out_date=payload.get("check_out_date"),
            num_guests=int(payload.get("num_guests") or 1),
            status=str(payload.get("status") or "reserved").strip() or "reserved",
            source_channel=str(payload.get("source_channel") or "web").strip() or "web",
            special_requests=str(payload.get("special_requests") or "").strip() or None,
            db_session=db_session,
        )
        return created if isinstance(created, dict) else {}
    except Exception as exc:
        logger.warning(
            "pre-booking auto-persist skipped due to error (session_id=%s): %s",
            str(getattr(context, "session_id", "") or "").strip(),
            str(exc),
        )
        return {}


@router.post("/form-submit")
@router.post("/submit-form")
@router.post("/form_submit")
async def submit_form(
    request: FormSubmitRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Create a ticket directly from form submission data.
    Accept submitted form values as-is and create the ticket.
    """
    from services.ticketing_service import ticketing_service
    from services.config_service import config_service

    resolved_hotel_code = config_service.resolve_hotel_code(request.hotel_code)
    request.hotel_code = resolved_hotel_code
    scope_token = db_config_service.set_hotel_context(resolved_hotel_code)
    try:
        requested_service_id = _normalize_service_identifier(request.service_id)
        service = config_service.get_service(requested_service_id)
        # DB fallback: service may exist only in the database (production mode)
        if not service:
            try:
                db_services = await db_config_service.get_services()
            except Exception:
                db_services = []
            for candidate in db_services:
                if _normalize_service_identifier(candidate.get("id")) == requested_service_id:
                    service = dict(candidate)
                    break
                if _normalize_service_identifier(candidate.get("name")) == requested_service_id:
                    service = dict(candidate)
                    break
        if not service:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Service '{request.service_id}' not found for hotel "
                    f"'{resolved_hotel_code}'."
                ),
            )
        if not bool(service.get("ticketing_enabled", False)):
            raise HTTPException(status_code=400, detail="Ticketing is not enabled for this service.")

        resolved_service_id = str(service.get("id") or requested_service_id).strip()
        form_data = request.form_data or {}
        req_meta = request.metadata or {}
        service_name = str(service.get("name") or resolved_service_id)

        issue_parts = [
            f"{k.replace('_', ' ').title()}: {v}"
            for k, v in form_data.items()
            if str(v).strip()
        ]
        issue_summary = "; ".join(issue_parts) or f"{service_name} booking request"

        phase = str(
            service.get("phase_id")
            or req_meta.get("phase") or req_meta.get("phase_id")
            or ""
        )

        department_id = str(service.get("department_id") or req_meta.get("department_id") or "")
        department_name = str(service.get("department_name") or req_meta.get("department_name") or "")

        context = await context_manager.get_or_create_context(
            session_id=request.session_id,
            hotel_code=resolved_hotel_code,
        )

        if isinstance(context.pending_data, dict):
            context.pending_data.update({
                k: str(v) for k, v in form_data.items() if str(v).strip()
            })
            context.pending_data["service_id"] = resolved_service_id
            context.pending_data["service_name"] = service_name
            if req_meta.get("guest_id"):
                context.pending_data.setdefault("guest_id", str(req_meta["guest_id"]))
            if req_meta.get("entity_id"):
                context.pending_data.setdefault("entity_id", str(req_meta["entity_id"]))
        else:
            context.pending_data = {
                k: str(v) for k, v in form_data.items() if str(v).strip()
            }

        room_number = str(
            form_data.get("room_number") or req_meta.get("room_number") or ""
        )
        if room_number:
            context.room_number = room_number

        ticket_payload = ticketing_service.build_lumira_ticket_payload(
            context=context,
            issue=issue_summary,
            message=issue_summary,
            category=service_name,
            priority=str(req_meta.get("priority") or "medium"),
            department_id=department_id,
            department_manager=department_name,
            phase=phase,
            source=str(req_meta.get("ticket_source") or "web_widget"),
        )

        ticket_payload["service_id"] = resolved_service_id
        ticket_payload["service_name"] = service_name

        # Merge pending_data (which includes trigger field values collected
        # before the form was shown) so they appear on the ticket as well.
        merged_form_data = dict(context.pending_data) if isinstance(context.pending_data, dict) else {}
        merged_form_data.update({k: str(v) for k, v in form_data.items() if str(v).strip()})

        for k, v in merged_form_data.items():
            ticket_payload.setdefault(k, str(v))

        try:
            result = await ticketing_service.create_ticket(ticket_payload)
        except Exception:
            logger.exception("form-submit: create_ticket raised an exception")
            return {
                "success": False,
                "ticket_id": "",
                "message": "Something went wrong while creating your request. Please try again.",
            }

        if result.success:
            confirm_msg = await _generate_confirmation_message(
                service_name=service_name,
                form_data=form_data,
                ticket_id=result.ticket_id or "assigned",
            )
        else:
            confirm_msg = (
                f"There was an issue submitting your {service_name} request."
                " Please try again or contact our staff."
            )

        try:
            from schemas.chat import MessageRole
            context.add_message(MessageRole.ASSISTANT, confirm_msg)
        except Exception:
            pass

        booking_info: dict[str, Any] = {}
        if result.success:
            booking_info = await _persist_prebooking_booking_if_possible(
                hotel_code=resolved_hotel_code,
                service_name=service_name,
                form_data=merged_form_data,
                req_meta=req_meta,
                context=context,
                db_session=db,
            )
            if booking_info:
                try:
                    context.booking_id = int(booking_info.get("booking_id"))
                except (TypeError, ValueError):
                    pass
                context.booking_confirmation_code = str(
                    booking_info.get("confirmation_code") or context.booking_confirmation_code or ""
                ).strip() or None
                context.booking_property_name = str(
                    booking_info.get("property_name") or context.booking_property_name or ""
                ).strip() or None
                context.booking_room_type = str(
                    booking_info.get("room_type") or context.booking_room_type or ""
                ).strip() or None
                context.booking_check_in_date = str(
                    booking_info.get("check_in_date") or context.booking_check_in_date or ""
                ).strip() or None
                context.booking_check_out_date = str(
                    booking_info.get("check_out_date") or context.booking_check_out_date or ""
                ).strip() or None
                context.booking_guest_name = str(
                    booking_info.get("guest_name") or context.booking_guest_name or ""
                ).strip() or None
                context.booking_phase = str(
                    booking_info.get("phase") or context.booking_phase or "pre_checkin"
                ).strip() or "pre_checkin"

                if not context.guest_name:
                    context.guest_name = str(booking_info.get("guest_name") or "").strip() or None
                if not context.guest_phone:
                    context.guest_phone = str(booking_info.get("guest_phone") or "").strip() or None
                if not context.room_number:
                    context.room_number = str(booking_info.get("room_number") or "").strip() or None

                if not isinstance(context.pending_data, dict):
                    context.pending_data = {}
                context.pending_data["_latest_booking_id"] = booking_info.get("booking_id")
                context.pending_data["_latest_booking_confirmation_code"] = str(
                    booking_info.get("confirmation_code") or ""
                ).strip()
                context.pending_data["_latest_booking_phase"] = str(
                    booking_info.get("phase") or "pre_checkin"
                ).strip()
                if str(booking_info.get("property_name") or "").strip():
                    context.pending_data["_stay_property_name"] = str(
                        booking_info.get("property_name") or ""
                    ).strip()
                integration_snapshot = context.pending_data.get("_integration", {})
                integration_update = dict(integration_snapshot) if isinstance(integration_snapshot, dict) else {}
                guest_id_text = str(booking_info.get("guest_id") or "").strip()
                guest_phone_text = str(booking_info.get("guest_phone") or "").strip()
                if guest_id_text:
                    integration_update.setdefault("guest_id", guest_id_text)
                    context.pending_data.setdefault("guest_id", guest_id_text)
                if guest_phone_text:
                    integration_update.setdefault("guest_phone", guest_phone_text)
                integration_update.setdefault("phase", "pre_booking")
                context.pending_data["_integration"] = integration_update

            # Clear booking flow state so the bot doesn't offer to "resume"
            # a booking that was already completed via form submission.
            from schemas.chat import ConversationState
            context.pending_action = None
            internal_keys = {
                k: v for k, v in (context.pending_data or {}).items()
                if isinstance(k, str) and k.startswith("_")
            }
            context.pending_data = internal_keys
            context.state = ConversationState.IDLE
            # Remove this service from suspended list if present
            if isinstance(context.suspended_services, list):
                context.suspended_services = [
                    s for s in context.suspended_services
                    if _normalize_service_identifier((s or {}).get("service_id")) != _normalize_service_identifier(resolved_service_id)
                ]
                if not context.suspended_services:
                    context.resume_prompt_sent = False
            try:
                await context_manager.save_context(context, db_session=db)
            except Exception:
                await context_manager.save_context(context, db_session=None)

            return {
                "success": True,
                "ticket_id": result.ticket_id or "",
                "message": confirm_msg,
                "booking_created": bool(booking_info),
                "booking_id": booking_info.get("booking_id") if booking_info else None,
                "booking_confirmation_code": booking_info.get("confirmation_code") if booking_info else "",
                "booking_phase": booking_info.get("phase") if booking_info else "",
            }

        raw_error = str(result.error or "").strip()
        if "<html" in raw_error.lower() or len(raw_error) > 300:
            clean_error = "The booking system is temporarily unavailable"
        else:
            clean_error = raw_error or "Unknown error"

        return {
            "success": False,
            "ticket_id": "",
            "message": (
                f"Submission failed: {clean_error}."
                " Please try again or contact our staff."
            ),
        }
    finally:
        db_config_service.reset_hotel_context(scope_token)


@router.post("/message", response_model=ChatResponse)
async def send_message(
    request: ChatRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
) -> ChatResponse:
    """
    Process a chat message and return bot response.
    DB session is injected so handlers can persist orders, guests, etc.
    """
    trace_id = getattr(http_request.state, "trace_id", "")
    _turn_start = _perf_counter()  # for new_detailed_logger duration tracking
    resolved_hotel_code = config_service.resolve_hotel_code(request.hotel_code)
    request.hotel_code = resolved_hotel_code
    scope_token = db_config_service.set_hotel_context(resolved_hotel_code)
    if not isinstance(request.metadata, dict):
        request.metadata = {}
    turn_trace_id, turn_token = turn_diagnostics_service.begin_turn(
        request=request,
        api_trace_id=trace_id,
    )
    if trace_id:
        request.metadata.setdefault("trace_id", trace_id)
    request.metadata.setdefault("turn_trace_id", turn_trace_id)
    _trace_chat_event(
        "chat_message_received",
        trace_id=trace_id,
        turn_trace_id=turn_trace_id,
        http_request=http_request,
        request=request,
        status_code=0,
        extra={
            "db_session_available": db is not None,
        },
    )
    # Log chat turn start to flow.log + new_detailed.log
    _chat_meta = request.metadata or {}
    _chat_phase = str(_chat_meta.get("phase_id") or _chat_meta.get("phase") or getattr(request, "phase_id", "") or "").strip()
    _chat_guest_info = {k: v for k, v in _chat_meta.items() if k in ("room_number", "guest_name", "guest_id", "entity_id") and v}
    try:
        from services.flow_logger import log_chat_turn
        meta = _chat_meta
        phase = _chat_phase
        guest_info = _chat_guest_info
        summary = str(meta.get("conversation_summary") or meta.get("summary") or "").strip()
        from services.config_service import config_service as _cs
        _all_svcs = _cs.get_services()
        try:
            db_services = await db_config_service.get_services()
            if isinstance(db_services, list) and db_services:
                _all_svcs = db_services
        except Exception:
            pass
        available_services = [str(s.get("id") or "") for s in (_all_svcs or []) if s.get("is_active", True)]
        log_chat_turn(
            session_id=str(request.session_id or ""),
            user_message=str(request.message or ""),
            phase=phase,
            available_services=available_services,
            context_history_count=len(meta.get("conversation_history") or []),
            guest_info=guest_info,
            summary=summary,
        )
    except Exception:
        pass
    # -- new_detailed_logger: chat turn IN --
    try:
        new_detailed_logger.log_chat_turn_in(
            session_id=str(request.session_id or ""),
            message=str(request.message or ""),
            hotel_code=str(request.hotel_code or ""),
            channel=str(getattr(request, "channel", "") or _chat_meta.get("channel") or ""),
            phase=_chat_phase,
            trace_id=trace_id,
            turn_trace_id=turn_trace_id,
            guest_info=_chat_guest_info or None,
            history_count=len(_chat_meta.get("conversation_history") or []),
            metadata=_chat_meta,
        )
    except Exception as _ndl_exc:
        import sys as _sys
        print(f"[new_detailed_logger] chat turn IN log failed: {_ndl_exc!r}", file=_sys.stderr, flush=True)

    try:
        response = await chat_service.process_message(request, db_session=db)
        await _attach_display_message(response)
        if not isinstance(response.metadata, dict):
            response.metadata = {}
        _enrich_service_llm_display_fields(response)
        try:
            await _inject_form_trigger(response, user_message=request.message)
        except Exception as _form_exc:
            import sys as _sys
            print(f"[form_trigger] EXCEPTION: {_form_exc!r}", file=_sys.stderr, flush=True)
        if trace_id:
            response.metadata.setdefault("trace_id", trace_id)
        response.metadata.setdefault("turn_trace_id", turn_trace_id)
        _record_evaluation_event(request, response, trace_id)
        # Log chat response to flow.log
        try:
            from services.flow_logger import log_chat_response, log_orchestration_decision
            resp_meta = response.metadata or {}
            routed_svc = str(resp_meta.get("response_source") or resp_meta.get("routing_path") or resp_meta.get("classified_intent") or "unknown")
            log_chat_response(
                session_id=str(request.session_id or ""),
                routed_service=routed_svc,
                final_response=str(response.message or ""),
            )
            orchestration = resp_meta.get("orchestration_decision") or resp_meta.get("decision")
            if orchestration and isinstance(orchestration, dict):
                log_orchestration_decision(
                    session_id=str(request.session_id or ""),
                    decision=orchestration,
                )
        except Exception:
            pass
        observability_service.log_event(
            "chat_message_processed",
            {
                "trace_id": trace_id,
                "session_id": request.session_id,
                "hotel_code": request.hotel_code,
                "channel": request.channel or (request.metadata or {}).get("channel") or "web",
                "intent": str((response.metadata or {}).get("classified_intent") or ""),
                "state": str(getattr(response.state, "value", response.state) or ""),
                "routing_path": str((response.metadata or {}).get("routing_path") or ""),
                "response_source": str((response.metadata or {}).get("response_source") or ""),
            },
        )
        conversation_audit_service.log_turn(
            request=request,
            response=response,
            trace_id=trace_id,
            db_fallback=False,
        )
        _trace_chat_event(
            "chat_message_processed",
            trace_id=trace_id,
            turn_trace_id=turn_trace_id,
            http_request=http_request,
            request=request,
            response=response,
            status_code=200,
            extra={
                "db_fallback": False,
            },
        )
        turn_diagnostics_service.log_turn_end(
            request=request,
            response=response,
            db_fallback=False,
            error="",
        )
        # -- new_detailed_logger: chat turn SUCCESS --
        try:
            _resp_meta = response.metadata or {}
            new_detailed_logger.log_chat_turn_out_success(
                session_id=str(request.session_id or ""),
                bot_reply=str(response.message or ""),
                display_message=str(getattr(response, "display_message", "") or ""),
                intent=str(_resp_meta.get("classified_intent") or ""),
                routing_path=str(_resp_meta.get("routing_path") or ""),
                response_source=str(_resp_meta.get("response_source") or ""),
                service_label=str(getattr(response, "service_llm_label", "") or ""),
                confidence=getattr(response, "service_llm_confidence", None),
                state=str(getattr(response.state, "value", response.state) or ""),
                duration_ms=round((_perf_counter() - _turn_start) * 1000, 2),
                trace_id=trace_id,
                turn_trace_id=turn_trace_id,
                db_fallback=False,
                ticket_created=bool(_resp_meta.get("ticket_created", False)),
                suggestions=list(_resp_meta.get("suggestions") or []) or None,
                orchestration=_resp_meta.get("orchestration_decision") or _resp_meta.get("decision") or None,
                full_metadata=_resp_meta,
            )
        except Exception as _ndl_exc:
            import sys as _sys
            print(f"[new_detailed_logger] chat turn SUCCESS log failed: {_ndl_exc!r}", file=_sys.stderr, flush=True)
        return response
    except OperationalError as e:
        db_first_mode = bool(getattr(settings, "chat_db_first_mode", False))
        allow_inmemory_fallback = bool(getattr(settings, "chat_db_allow_inmemory_fallback", True))
        if db_first_mode and not allow_inmemory_fallback:
            observability_service.log_event(
                "chat_message_failed_db_required",
                {
                    "trace_id": trace_id,
                    "session_id": request.session_id,
                    "hotel_code": request.hotel_code,
                    "error": str(e),
                },
            )
            conversation_audit_service.log_failed_turn(
                request=request,
                trace_id=trace_id,
                error=str(e),
                db_fallback=False,
            )
            _trace_chat_event(
                "chat_message_failed_db_required",
                trace_id=trace_id,
                turn_trace_id=turn_trace_id,
                http_request=http_request,
                request=request,
                status_code=503,
                error=str(e),
                extra={
                    "db_fallback": False,
                    "db_first_mode": True,
                    "db_required": True,
                },
            )
            turn_diagnostics_service.log_turn_end(
                request=request,
                response=None,
                db_fallback=False,
                error=f"db_required: {str(e)}",
            )
            # â”€â”€ new_detailed_logger: failed â€” db_required â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            try:
                new_detailed_logger.log_chat_turn_out_failed(
                    session_id=str(request.session_id or ""),
                    message=str(request.message or ""),
                    hotel_code=str(request.hotel_code or ""),
                    trace_id=trace_id,
                    turn_trace_id=turn_trace_id,
                    duration_ms=round((_perf_counter() - _turn_start) * 1000, 2),
                    error_type="OperationalError(db_required)",
                    error_msg=str(e),
                    tb=_traceback.format_exc(),
                    db_fallback=False,
                    http_status=503,
                )
            except Exception:
                pass
            raise HTTPException(
                status_code=503,
                detail="Database is unavailable. DB-first mode requires database connectivity.",
            )

        print(f"API DB Error (fallback to in-memory): {e}")
        try:
            response = await chat_service.process_message(request, db_session=None)
            await _attach_display_message(response)
            if not isinstance(response.metadata, dict):
                response.metadata = {}
            _enrich_service_llm_display_fields(response)
            try:
                await _inject_form_trigger(response, user_message=request.message)
            except Exception:
                pass
            response.metadata["db_fallback"] = True
            if trace_id:
                response.metadata.setdefault("trace_id", trace_id)
            response.metadata.setdefault("turn_trace_id", turn_trace_id)
            _record_evaluation_event(request, response, trace_id)
            observability_service.log_event(
                "chat_message_processed_db_fallback",
                {
                    "trace_id": trace_id,
                    "session_id": request.session_id,
                    "hotel_code": request.hotel_code,
                    "error": str(e),
                },
            )
            conversation_audit_service.log_turn(
                request=request,
                response=response,
                trace_id=trace_id,
                db_fallback=True,
            )
            _trace_chat_event(
                "chat_message_processed_db_fallback",
                trace_id=trace_id,
                turn_trace_id=turn_trace_id,
                http_request=http_request,
                request=request,
                response=response,
                status_code=200,
                extra={
                    "db_fallback": True,
                    "primary_db_error": str(e),
                },
            )
            turn_diagnostics_service.log_turn_end(
                request=request,
                response=response,
                db_fallback=True,
                error=f"primary_db_failed: {str(e)}",
            )
            # â”€â”€ new_detailed_logger: SUCCESS via DB-fallback path â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            try:
                _resp_meta = response.metadata or {}
                new_detailed_logger.log_chat_turn_out_success(
                    session_id=str(request.session_id or ""),
                    bot_reply=str(response.message or ""),
                    display_message=str(getattr(response, "display_message", "") or ""),
                    intent=str(_resp_meta.get("classified_intent") or ""),
                    routing_path=str(_resp_meta.get("routing_path") or ""),
                    response_source=str(_resp_meta.get("response_source") or ""),
                    service_label=str(getattr(response, "service_llm_label", "") or ""),
                    confidence=getattr(response, "service_llm_confidence", None),
                    state=str(getattr(response.state, "value", response.state) or ""),
                    duration_ms=round((_perf_counter() - _turn_start) * 1000, 2),
                    trace_id=trace_id,
                    turn_trace_id=turn_trace_id,
                    db_fallback=True,
                    ticket_created=bool(_resp_meta.get("ticket_created", False)),
                    suggestions=list(_resp_meta.get("suggestions") or []) or None,
                    orchestration=_resp_meta.get("orchestration_decision") or None,
                    full_metadata=_resp_meta,
                )
            except Exception:
                pass
            return response
        except Exception as fallback_error:
            print(f"API Fallback Error: {fallback_error}")
            observability_service.log_event(
                "chat_message_fallback_failed",
                {
                    "trace_id": getattr(http_request.state, "trace_id", ""),
                    "session_id": request.session_id,
                    "hotel_code": request.hotel_code,
                    "error": str(fallback_error),
                },
            )
            conversation_audit_service.log_failed_turn(
                request=request,
                trace_id=trace_id,
                error=str(fallback_error),
                db_fallback=True,
            )
            _trace_chat_event(
                "chat_message_failed_db_fallback",
                trace_id=trace_id,
                turn_trace_id=turn_trace_id,
                http_request=http_request,
                request=request,
                status_code=503,
                error=str(fallback_error),
                extra={
                    "db_fallback": True,
                    "primary_db_error": str(e),
                },
            )
            turn_diagnostics_service.log_turn_end(
                request=request,
                response=None,
                db_fallback=True,
                error=str(fallback_error),
            )
            # â”€â”€ new_detailed_logger: failed â€” db_fallback_error â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            try:
                new_detailed_logger.log_chat_turn_out_failed(
                    session_id=str(request.session_id or ""),
                    message=str(request.message or ""),
                    hotel_code=str(request.hotel_code or ""),
                    trace_id=trace_id,
                    turn_trace_id=turn_trace_id,
                    duration_ms=round((_perf_counter() - _turn_start) * 1000, 2),
                    error_type=type(fallback_error).__name__,
                    error_msg=str(fallback_error),
                    tb=_traceback.format_exc(),
                    db_fallback=True,
                    http_status=503,
                )
            except Exception:
                pass
            raise HTTPException(
                status_code=503,
                detail="Database is unavailable and fallback processing failed.",
            )
    except Exception as e:
        print(f"API Error: {e}")
        _traceback.print_exc()
        observability_service.log_event(
            "chat_message_failed",
            {
                "trace_id": getattr(http_request.state, "trace_id", ""),
                "session_id": request.session_id,
                "hotel_code": request.hotel_code,
                "error": str(e),
            },
        )
        conversation_audit_service.log_failed_turn(
            request=request,
            trace_id=trace_id,
            error=str(e),
            db_fallback=False,
        )
        _trace_chat_event(
            "chat_message_failed",
            trace_id=trace_id,
            turn_trace_id=turn_trace_id,
            http_request=http_request,
            request=request,
            status_code=500,
            error=str(e),
            extra={
                "db_fallback": False,
            },
        )
        turn_diagnostics_service.log_turn_end(
            request=request,
            response=None,
            db_fallback=False,
            error=str(e),
        )
        # â”€â”€ new_detailed_logger: failed â€” general exception â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        try:
            new_detailed_logger.log_chat_turn_out_failed(
                session_id=str(request.session_id or ""),
                message=str(request.message or ""),
                hotel_code=str(request.hotel_code or ""),
                trace_id=trace_id,
                turn_trace_id=turn_trace_id,
                duration_ms=round((_perf_counter() - _turn_start) * 1000, 2),
                error_type=type(e).__name__,
                error_msg=str(e),
                tb=_traceback.format_exc(),
                db_fallback=False,
                http_status=500,
            )
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db_config_service.reset_hotel_context(scope_token)
        turn_diagnostics_service.clear_turn(turn_token)


@router.get("/session/{session_id}")
async def get_session(session_id: str, db: AsyncSession = Depends(get_db)):
    """
    Get session details and conversation history.
    """
    db_fallback = False
    try:
        context = await context_manager.get_context(session_id, db_session=db)
    except OperationalError as e:
        if bool(getattr(settings, "chat_db_first_mode", False)) and not bool(
            getattr(settings, "chat_db_allow_inmemory_fallback", True)
        ):
            raise HTTPException(
                status_code=503,
                detail="Database is unavailable. DB-first mode requires database connectivity.",
            )
        print(f"API DB Error (get_session fallback to local store): {e}")
        context = await context_manager.get_context(session_id, db_session=None)
        db_fallback = True
    if context is None:
        raise HTTPException(status_code=404, detail="Session not found")

    payload = {
        "session_id": context.session_id,
        "state": context.state.value,
        "hotel_code": context.hotel_code,
        "guest_phone": context.guest_phone,
        "room_number": context.room_number,
        "channel": context.channel,
        "message_count": len(context.messages),
        "messages": [
            {
                "role": msg.role.value,
                "content": msg.content,
                "timestamp": msg.timestamp.isoformat(),
                "metadata": msg.metadata,
            }
            for msg in context.messages
        ],
        "pending_action": context.pending_action,
        "pending_data": context.pending_data,
        "created_at": context.created_at.isoformat(),
        "updated_at": context.updated_at.isoformat(),
    }
    if db_fallback:
        payload["db_fallback"] = True
    return payload


@router.delete("/session/{session_id}")
async def delete_session(session_id: str, db: AsyncSession = Depends(get_db)):
    """
    Delete a session and its history.
    """
    db_fallback = False
    try:
        deleted = await context_manager.delete_context(session_id, db_session=db)
    except OperationalError as e:
        if bool(getattr(settings, "chat_db_first_mode", False)) and not bool(
            getattr(settings, "chat_db_allow_inmemory_fallback", True)
        ):
            raise HTTPException(
                status_code=503,
                detail="Database is unavailable. DB-first mode requires database connectivity.",
            )
        print(f"API DB Error (delete_session fallback to local store): {e}")
        deleted = await context_manager.delete_context(session_id, db_session=None)
        db_fallback = True
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"message": "Session deleted", "session_id": session_id, "db_fallback": db_fallback}


@router.get("/sessions")
async def list_sessions(db: AsyncSession = Depends(get_db)):
    """
    List all active sessions.
    """
    db_fallback = False
    try:
        sessions = await context_manager.list_sessions(db_session=db)
    except OperationalError as e:
        if bool(getattr(settings, "chat_db_first_mode", False)) and not bool(
            getattr(settings, "chat_db_allow_inmemory_fallback", True)
        ):
            raise HTTPException(
                status_code=503,
                detail="Database is unavailable. DB-first mode requires database connectivity.",
            )
        print(f"API DB Error (list_sessions fallback to local store): {e}")
        sessions = await context_manager.list_sessions(db_session=None)
        db_fallback = True

    summaries = []
    for sid in sessions:
        summary_db_session = None if db_fallback else db
        try:
            summary = await context_manager.get_conversation_summary(
                sid, db_session=summary_db_session
            )
        except OperationalError:
            if bool(getattr(settings, "chat_db_first_mode", False)) and not bool(
                getattr(settings, "chat_db_allow_inmemory_fallback", True)
            ):
                raise HTTPException(
                    status_code=503,
                    detail="Database is unavailable. DB-first mode requires database connectivity.",
                )
            summary = await context_manager.get_conversation_summary(sid, db_session=None)
            db_fallback = True
        summaries.append(summary)
    return {"sessions": summaries, "count": len(summaries), "db_fallback": db_fallback}


@router.post("/session/{session_id}/reset")
async def reset_session(session_id: str, db: AsyncSession = Depends(get_db)):
    """
    Reset a session's state to IDLE without deleting history.
    """
    from schemas.chat import ConversationState
    db_fallback = False

    try:
        context = await context_manager.get_context(session_id, db_session=db)
        if context is None:
            raise HTTPException(status_code=404, detail="Session not found")
        context.state = ConversationState.IDLE
        context.pending_action = None
        context.pending_data = {}
        await context_manager.save_context(context, db_session=db)
    except OperationalError as e:
        if bool(getattr(settings, "chat_db_first_mode", False)) and not bool(
            getattr(settings, "chat_db_allow_inmemory_fallback", True)
        ):
            raise HTTPException(
                status_code=503,
                detail="Database is unavailable. DB-first mode requires database connectivity.",
            )
        print(f"API DB Error (reset_session fallback to local store): {e}")
        context = await context_manager.get_context(session_id, db_session=None)
        if context is None:
            raise HTTPException(status_code=404, detail="Session not found")
        context.state = ConversationState.IDLE
        context.pending_action = None
        context.pending_data = {}
        await context_manager.save_context(context, db_session=None)
        db_fallback = True

    return {"message": "Session reset", "state": "idle", "db_fallback": db_fallback}


@router.get("/properties")
async def list_properties(db: AsyncSession = Depends(get_db)):
    """Return available property codes for chat UI selection."""
    properties_by_code: dict[str, dict[str, str]] = {}
    try:
        rows = (
            await db.execute(
                select(Hotel).where(Hotel.is_active == True).order_by(Hotel.code.asc())  # noqa: E712
            )
        ).scalars().all()
        for row in rows:
            code = str(row.code or "").strip()
            if not code:
                continue
            normalized_code = str(code).strip().lower().replace(" ", "_")
            properties_by_code[normalized_code] = {
                "code": normalized_code,
                "name": str(row.name or code).strip(),
                "city": str(row.city or "").strip(),
            }
    except Exception:
        pass

    # Merge property config files as a resilient fallback source.
    try:
        properties_dir = Path(__file__).resolve().parent.parent.parent / "config" / "properties"
        if properties_dir.exists() and properties_dir.is_dir():
            for json_file in sorted(properties_dir.glob("*.json")):
                code = str(json_file.stem or "").strip().lower().replace(" ", "_")
                if not code:
                    continue
                name = code
                city = ""
                try:
                    payload = json.loads(json_file.read_text(encoding="utf-8"))
                    business = payload.get("business", {}) if isinstance(payload, dict) else {}
                    business_id = str(business.get("id") or "").strip().lower().replace(" ", "_")
                    if business_id and business_id != "default":
                        code = business_id
                    name = str(business.get("name") or code).strip()
                    city = str(business.get("city") or "").strip()
                except Exception:
                    pass
                properties_by_code.setdefault(
                    code,
                    {
                        "code": code,
                        "name": name or code,
                        "city": city,
                    },
                )
    except Exception:
        pass

    properties: list[dict[str, str]] = sorted(
        properties_by_code.values(),
        key=lambda row: str(row.get("code") or ""),
    )

    if not properties:
        business = config_service.get_business_info()
        fallback_code = config_service.resolve_hotel_code(
            str(business.get("id") or "").strip() or "DEFAULT"
        )
        properties = [
            {
                "code": fallback_code,
                "name": str(business.get("name") or "Default Property").strip(),
                "city": str(business.get("city") or "").strip(),
            }
        ]

    return {"properties": properties}


@router.get("/test-profiles")
async def get_chat_test_phase_profiles(hotel_code: Optional[str] = "DEFAULT"):
    """
    Return phase-to-profile mapping used by the chat test UI to auto-apply
    guest/entity metadata for dashboard ticket validation.
    """
    resolved_hotel_code = config_service.resolve_hotel_code(hotel_code)
    scope_token = db_config_service.set_hotel_context(resolved_hotel_code)
    try:
        try:
            configured_phases = config_service.get_journey_phases()
        except Exception:
            configured_phases = []

        phases_payload: list[dict[str, str]] = []
        if isinstance(configured_phases, list):
            for phase in configured_phases:
                if not isinstance(phase, dict):
                    continue
                phase_id = _normalize_phase_identifier(phase.get("id"))
                if not phase_id:
                    continue
                phase_name = str(phase.get("name") or "").strip() or phase_id.replace("_", " ").title()
                phases_payload.append({"id": phase_id, "name": phase_name})

        if not phases_payload:
            phases_payload = [
                {"id": "pre_booking", "name": "Pre Booking"},
                {"id": "pre_checkin", "name": "Pre Checkin"},
                {"id": "during_stay", "name": "During Stay"},
                {"id": "post_checkout", "name": "Post Checkout"},
            ]

        return {
            "auto_apply_enabled": bool(getattr(settings, "chat_test_phase_profile_auto_apply", True)),
            "profiles_by_phase": _load_chat_test_phase_profiles(),
            "phases": phases_payload,
        }
    finally:
        db_config_service.reset_hotel_context(scope_token)


class SuggestionsRequest(BaseModel):
    last_bot_message: str
    user_message: Optional[str] = None
    hotel_code: Optional[str] = "DEFAULT"
    current_phase: Optional[str] = "pre_booking"
    session_id: Optional[str] = None
    fallback_suggestions: list[str] = Field(default_factory=list)


@router.post("/suggestions")
async def get_contextual_suggestions(
    request: SuggestionsRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Contextual suggestion endpoint used by suggestion bubbles.
    It combines live session context + phase-aware service context + full KB/service KB
    and asks an LLM to emit only grounded, answerable next-message suggestions.
    """
    hotel_code = config_service.resolve_hotel_code(request.hotel_code)
    scope_token = db_config_service.set_hotel_context(hotel_code)
    try:
        phase_id = _normalize_phase_identifier(request.current_phase or "pre_booking") or "pre_booking"
        active_service_id = ""
        active_service: dict[str, Any] = {}
        conversation_state = ""
        conversation_history: list[dict[str, str]] = []
        pending_data_public: dict[str, Any] = {}

        cap_summary = config_service.get_capability_summary(hotel_code)
        raw_caps = cap_summary.get("capabilities", {})
        if not isinstance(raw_caps, dict):
            raw_caps = {}
        nlu_policy = cap_summary.get("nlu_policy", {})
        if not isinstance(nlu_policy, dict):
            nlu_policy = {}
        runtime_overlay_db = True
        try:
            db_caps = await db_config_service.get_capabilities()
            if isinstance(db_caps, dict):
                raw_caps = db_caps
        except Exception:
            pass
        if runtime_overlay_db:
            try:
                db_knowledge = await db_config_service.get_knowledge_config()
                if isinstance(db_knowledge, dict):
                    db_nlu_policy = db_knowledge.get("nlu_policy", {})
                    if isinstance(db_nlu_policy, dict):
                        nlu_policy = db_nlu_policy
            except Exception:
                pass

        if request.session_id:
            try:
                context = await context_manager.get_context(request.session_id, db_session=db)
                if context:
                    conversation_state = str(context.state.value if context.state else "").strip()
                    pending_action = str(context.pending_action or "").strip()
                    pending_data_raw = context.pending_data if isinstance(context.pending_data, dict) else {}
                    pending_data_public = {
                        key: value
                        for key, value in pending_data_raw.items()
                        if isinstance(key, str) and not key.startswith("_")
                    }

                    phase_candidates = [
                        pending_data_public.get("phase"),
                        (pending_data_raw.get("_integration") or {}).get("phase")
                        if isinstance(pending_data_raw.get("_integration"), dict)
                        else "",
                        pending_data_raw.get("_selected_phase_id"),
                        (pending_data_raw.get("_selected_phase") or {}).get("id")
                        if isinstance(pending_data_raw.get("_selected_phase"), dict)
                        else "",
                    ]
                    for candidate in phase_candidates:
                        normalized = _normalize_phase_identifier(candidate)
                        if normalized:
                            phase_id = normalized
                            break

                    active_service_candidates = [
                        pending_data_public.get("service_id"),
                        pending_data_public.get("service"),
                        pending_data_raw.get("_last_service_id"),
                        pending_action,
                    ]
                    for candidate in active_service_candidates:
                        normalized_service = _normalize_service_identifier(candidate)
                        if not normalized_service:
                            continue
                        svc = config_service.get_service(normalized_service)
                        if not svc:
                            continue
                        active_service_id = normalized_service
                        active_service = {
                            "id": str(svc.get("id") or normalized_service).strip(),
                            "name": str(svc.get("name") or normalized_service).strip(),
                            "type": str(svc.get("type") or "service").strip(),
                            "description": str(svc.get("description") or "").strip(),
                        }
                        break

                    if hasattr(context, "get_recent_messages"):
                        total_chars = 0
                        for msg in context.get_recent_messages(12):
                            content = str(msg.content or "").strip()
                            if not content or total_chars >= 6000:
                                break
                            clipped = content[: min(6000 - total_chars, 850)]
                            conversation_history.append(
                                {
                                    "role": str(getattr(msg.role, "value", "") or "").strip(),
                                    "content": clipped,
                                }
                            )
                            total_chars += len(clipped)
            except Exception:
                pass

        phases = config_service.get_journey_phases()
        if runtime_overlay_db:
            try:
                db_phases = await db_config_service.get_journey_phases()
                if isinstance(db_phases, list) and db_phases:
                    phases = db_phases
            except Exception:
                pass
        if not isinstance(phases, list):
            phases = []
        phase_obj = next(
            (
                p
                for p in phases
                if isinstance(p, dict) and _normalize_phase_identifier(p.get("id")) == phase_id
            ),
            None,
        )
        phase_name = (
            str((phase_obj or {}).get("name") or "").strip()
            if isinstance(phase_obj, dict)
            else ""
        ) or phase_id.replace("_", " ").title()
        phase_description = (
            str((phase_obj or {}).get("description") or "").strip()
            if isinstance(phase_obj, dict)
            else ""
        )

        service_kb_records = cap_summary.get("service_kb_records", [])
        if runtime_overlay_db:
            try:
                db_service_kb_records = await db_config_service.get_service_kb_records(active_only=True)
                if isinstance(db_service_kb_records, list):
                    service_kb_records = config_service.summarize_service_kb_records(
                        db_service_kb_records,
                        limit=120,
                    )
            except Exception:
                pass
        if not isinstance(service_kb_records, list):
            service_kb_records = []
        service_kb_by_service: dict[str, dict[str, Any]] = {}
        for record in service_kb_records:
            if not isinstance(record, dict):
                continue
            sid = _normalize_service_identifier(record.get("service_id"))
            if not sid:
                continue
            existing = service_kb_by_service.get(sid)
            try:
                version = int(record.get("version") or 0)
            except (TypeError, ValueError):
                version = 0
            try:
                existing_version = int((existing or {}).get("version") or 0)
            except (TypeError, ValueError):
                existing_version = 0
            if existing is None or version >= existing_version:
                service_kb_by_service[sid] = dict(record)

        service_catalog = cap_summary.get("service_catalog", [])
        if runtime_overlay_db:
            try:
                db_services = await db_config_service.get_services()
                if isinstance(db_services, list):
                    service_catalog = db_services
            except Exception:
                pass
        if not isinstance(service_catalog, list):
            service_catalog = []
        if not service_catalog:
            service_catalog = config_service.get_services()

        phase_services_context: list[dict[str, Any]] = []
        out_of_phase_services_context: list[dict[str, Any]] = []
        for service in service_catalog:
            if not isinstance(service, dict):
                continue
            sid = _normalize_service_identifier(service.get("id"))
            if not sid or not bool(service.get("is_active", True)):
                continue
            service_phase_id = _normalize_phase_identifier(service.get("phase_id"))
            service_phase_name = next(
                (
                    str(phase.get("name") or "").strip()
                    for phase in phases
                    if isinstance(phase, dict) and _normalize_phase_identifier(phase.get("id")) == service_phase_id
                ),
                service_phase_id.replace("_", " ").title() if service_phase_id else "",
            )
            prompt_pack = service.get("service_prompt_pack")
            if not isinstance(prompt_pack, dict):
                prompt_pack = {}
            required_slots = prompt_pack.get("required_slots", [])
            if not isinstance(required_slots, list):
                required_slots = []
            required_slot_labels: list[str] = []
            for slot in required_slots[:8]:
                if not isinstance(slot, dict):
                    continue
                label = str(slot.get("label") or slot.get("id") or "").strip()
                if label:
                    required_slot_labels.append(label)

            kb_record = service_kb_by_service.get(sid, {})
            extracted_knowledge = (
                str(kb_record.get("extracted_knowledge") or "").strip()
                or str(prompt_pack.get("extracted_knowledge") or "").strip()
            )
            row = {
                "id": sid,
                "name": str(service.get("name") or sid).strip(),
                "type": str(service.get("type") or "service").strip(),
                "description": str(service.get("description") or "").strip(),
                "phase_id": service_phase_id,
                "phase_name": service_phase_name,
                "profile": str(prompt_pack.get("profile") or service.get("profile") or "").strip(),
                "ticketing_enabled": bool(service.get("ticketing_enabled", True)),
                "ticketing_policy": str(service.get("ticketing_policy") or "").strip(),
                "required_slots": required_slot_labels,
                "knowledge_facts": _service_kb_fact_lines(kb_record, max_items=18),
                "extracted_knowledge": extracted_knowledge[:2800],
            }
            if service_phase_id == phase_id:
                phase_services_context.append(row)
            else:
                out_of_phase_services_context.append(row)

        if not active_service and active_service_id:
            for row in phase_services_context + out_of_phase_services_context:
                if _normalize_service_identifier(row.get("id")) != active_service_id:
                    continue
                active_service = {
                    "id": row.get("id", ""),
                    "name": row.get("name", ""),
                    "type": row.get("type", ""),
                    "description": row.get("description", ""),
                }
                break

        full_kb_budget = max(38000, int(getattr(settings, "full_kb_llm_max_kb_chars", 180000) or 180000))
        kb_text = str(
            config_service.get_full_kb_text_with_sources(
                max_chars=full_kb_budget,
                max_sources=200,
            ) or ""
        ).strip()
        db_kb_text = await _load_all_kb_text_from_db(
            db=db,
            hotel_code=hotel_code,
            max_chars=full_kb_budget,
        )
        prefer_db_kb = bool(getattr(settings, "chat_db_prefer_kb_content", False))
        if prefer_db_kb and db_kb_text:
            kb_text = db_kb_text
        elif not kb_text and db_kb_text:
            kb_text = db_kb_text
        elif db_kb_text and len(kb_text) < 2500:
            kb_text = (kb_text + "\n\n" + db_kb_text).strip()[:full_kb_budget]

        service_knowledge_parts: list[str] = []
        seen_ids: set[str] = set()
        for row in phase_services_context[:24]:
            sid = _normalize_service_identifier(row.get("id"))
            if not sid or sid in seen_ids:
                continue
            seen_ids.add(sid)
            facts = row.get("knowledge_facts", [])
            facts_text = "\n".join(
                f"- {str(fact).strip()}"
                for fact in facts
                if str(fact).strip()
            )
            extracted = str(row.get("extracted_knowledge") or "").strip()
            block = (
                f"[Service: {row.get('name', sid)} | id={sid}]\n"
                f"Description: {str(row.get('description') or '').strip()}\n"
                f"Facts:\n{facts_text or '- (none)'}\n"
                f"Extracted Knowledge:\n{extracted or '(none)'}"
            ).strip()
            service_knowledge_parts.append(block[:3500])
        if active_service_id and active_service_id not in seen_ids:
            active_record = service_kb_by_service.get(active_service_id, {})
            active_extracted = str(active_record.get("extracted_knowledge") or "").strip()
            active_facts = "\n".join(
                f"- {line}" for line in _service_kb_fact_lines(active_record, max_items=18)
            )
            if active_extracted or active_facts:
                service_knowledge_parts.append(
                    (
                        f"[Active Service Knowledge: {active_service_id}]\n"
                        f"Facts:\n{active_facts or '- (none)'}\n"
                        f"Extracted Knowledge:\n{active_extracted or '(none)'}"
                    )[:3500]
                )
        service_knowledge_corpus = "\n\n---\n\n".join(service_knowledge_parts)[:26000]

        enabled_capabilities = [
            cap_id
            for cap_id, cap_data in raw_caps.items()
            if isinstance(cap_data, dict) and cap_data.get("enabled", False)
        ]
        property_constraints = [
            str(d).strip()
            for d in (nlu_policy.get("donts", []) if isinstance(nlu_policy, dict) else [])
            if str(d).strip()
        ]
        bot_delivery_boundary = {
            "medium": (
                "text only; no image/video delivery; no real-time availability/pricing "
                "from external systems outside active configured services"
            ),
            "enabled_capabilities": enabled_capabilities,
            "property_constraints": property_constraints,
        }

        fallback_candidates = _sanitize_suggestions(
            [str(item).strip() for item in (request.fallback_suggestions or []) if str(item).strip()],
            limit=6,
        )
        context_payload = {
            "last_bot_message": str(request.last_bot_message or "").strip(),
            "user_message": str(request.user_message or "").strip(),
            "conversation_history": conversation_history,
            "conversation_state": conversation_state,
            "pending_data_public": pending_data_public,
            "journey_phase": {
                "id": phase_id,
                "name": phase_name,
                "description": phase_description,
            },
            "active_service": active_service,
            "services_allowed_in_phase": phase_services_context[:40],
            "services_outside_phase": out_of_phase_services_context[:80],
            "knowledge_base": {
                "full_kb_text": kb_text,
                "service_knowledge_corpus": service_knowledge_corpus,
                "service_kb_record_count": len(service_kb_records),
            },
            "candidate_suggestions_from_runtime": fallback_candidates,
            "bot_delivery_boundary": bot_delivery_boundary,
        }

        system_prompt = (
            "You generate suggestion chips for a hotel concierge chatbot.\n"
            "Goal: produce exactly 3 guest messages that the user is likely to send next and that this bot can answer positively right now.\n\n"
            "Strict grounding workflow:\n"
            "1. Respect `journey_phase` and only `services_allowed_in_phase` for service asks.\n"
            "2. Use `knowledge_base.full_kb_text` and `knowledge_base.service_knowledge_corpus` as source of truth.\n"
            "3. If information is absent/uncertain, do NOT suggest that topic.\n"
            "4. Reject out-of-phase or unsupported runtime candidates.\n"
            "5. Prefer suggestions that can receive a direct helpful answer now.\n"
            "6. If `active_service` exists, prioritize continuation suggestions.\n\n"
            "Style:\n"
            "- First-person guest chat text.\n"
            "- Questions or requests only; never data submission.\n"
            "- 2 to 10 words.\n"
            "- No personal placeholders or unique personal values.\n\n"
            "Return ONLY strict JSON in this schema:\n"
            "{\"suggestions\":[{\"text\":\"...\",\"supported\":true,\"evidence\":[\"short snippet\"]},{\"text\":\"...\",\"supported\":true,\"evidence\":[\"short snippet\"]},{\"text\":\"...\",\"supported\":true,\"evidence\":[\"short snippet\"]}]}"
        )

        result = await llm_client.chat_with_json(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(context_payload, ensure_ascii=False)},
            ],
            temperature=0.2,
        )
        suggestions = _sanitize_suggestions(
            _extract_suggestion_candidates(result.get("suggestions", [])),
            limit=4,
        )
        if not suggestions and fallback_candidates:
            suggestions = _sanitize_suggestions(fallback_candidates, limit=4)
        return {"suggestions": suggestions}
    except Exception as e:
        print(f"Suggestions endpoint error: {e}")
        fallback = _sanitize_suggestions(
            [str(item).strip() for item in (request.fallback_suggestions or []) if str(item).strip()],
            limit=4,
        )
        return {"suggestions": fallback}
    finally:
        db_config_service.reset_hotel_context(scope_token)
