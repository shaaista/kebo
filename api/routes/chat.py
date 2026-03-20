"""
Chat API Routes

Endpoints for chat functionality and testing.
"""

import json
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
from config.settings import settings
from core.context_manager import context_manager
from models.database import KBFile, Hotel, get_db
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import OperationalError


router = APIRouter(prefix="/api/chat", tags=["Chat"])

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


async def _load_latest_kb_text_from_db(
    *,
    db: AsyncSession,
    hotel_code: str,
    max_chars: int = 50000,
) -> str:
    """Best-effort DB fallback for KB text when file-sourced KB is stale/empty."""
    code = str(hotel_code or "DEFAULT").strip() or "DEFAULT"
    hotel_id: Optional[int] = None
    try:
        result = await db.execute(
            select(Hotel.id).where(Hotel.code == code).limit(1)
        )
        hotel_id = result.scalar_one_or_none()
        if hotel_id is None and code != "DEFAULT":
            fallback = await db.execute(
                select(Hotel.id).where(Hotel.code == "DEFAULT").limit(1)
            )
            hotel_id = fallback.scalar_one_or_none()
        if hotel_id is None:
            any_hotel = await db.execute(select(Hotel.id).order_by(Hotel.id.asc()).limit(1))
            hotel_id = any_hotel.scalar_one_or_none()
        if hotel_id is None:
            return ""

        row = await db.execute(
            select(KBFile.content)
            .where(KBFile.hotel_id == hotel_id)
            .order_by(KBFile.id.desc())
            .limit(1)
        )
        content = str(row.scalar_one_or_none() or "").strip()
        if not content:
            return ""
        if max_chars and max_chars > 0:
            return content[:max_chars]
        return content
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
    # Log chat turn start to flow.log
    try:
        from services.flow_logger import log_chat_turn
        meta = request.metadata or {}
        phase = str(meta.get("phase_id") or meta.get("phase") or request.phase_id or "").strip()  # type: ignore[attr-defined]
        guest_info = {k: v for k, v in meta.items() if k in ("room_number", "guest_name", "guest_id", "entity_id") and v}
        summary = str(meta.get("conversation_summary") or meta.get("summary") or "").strip()
        from services.config_service import config_service as _cs
        _all_svcs = _cs.get_services()
        if bool(getattr(settings, "chat_db_overlay_runtime_config", False)):
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

    try:
        response = await chat_service.process_message(request, db_session=db)
        await _attach_display_message(response)
        if not isinstance(response.metadata, dict):
            response.metadata = {}
        _enrich_service_llm_display_fields(response)
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
            raise HTTPException(
                status_code=503,
                detail="Database is unavailable and fallback processing failed.",
            )
    except Exception as e:
        print(f"API Error: {e}")
        import traceback
        traceback.print_exc()
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
        raise HTTPException(status_code=500, detail=str(e))
    finally:
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


@router.get("/test-profiles")
async def get_chat_test_phase_profiles():
    """
    Return phase-to-profile mapping used by the chat test UI to auto-apply
    guest/entity metadata for dashboard ticket validation.
    """
    try:
        from services.config_service import config_service

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
    try:
        from services.config_service import config_service

        hotel_code = str(request.hotel_code or "DEFAULT").strip() or "DEFAULT"
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
        runtime_overlay_db = bool(getattr(settings, "chat_db_overlay_runtime_config", False))
        if runtime_overlay_db:
            try:
                db_caps = await db_config_service.get_capabilities()
                if isinstance(db_caps, dict):
                    raw_caps = db_caps
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

        kb_text = str(config_service.get_full_kb_text(max_chars=38000) or "").strip()
        db_kb_text = await _load_latest_kb_text_from_db(
            db=db,
            hotel_code=hotel_code,
            max_chars=42000,
        )
        prefer_db_kb = bool(getattr(settings, "chat_db_prefer_kb_content", False))
        if prefer_db_kb and db_kb_text:
            kb_text = db_kb_text
        elif not kb_text and db_kb_text:
            kb_text = db_kb_text
        elif db_kb_text and len(kb_text) < 2500:
            kb_text = (kb_text + "\n\n" + db_kb_text).strip()[:42000]

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
