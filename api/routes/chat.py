"""
Chat API Routes

Endpoints for chat functionality and testing.
"""

import json
from fastapi import APIRouter, HTTPException, Depends, Request
from typing import Any, Optional, List
from pydantic import BaseModel

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
from config.settings import settings
from core.context_manager import context_manager
from models.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import OperationalError


router = APIRouter(prefix="/api/chat", tags=["Chat"])

_DEFAULT_CHAT_TEST_PHASE_PROFILES: dict[str, dict[str, str]] = {
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

    # Profiles are only valid when guest_id is present.
    if not str(normalized.get("guest_id") or "").strip():
        return {}

    return normalized


def _load_chat_test_phase_profiles() -> dict[str, dict[str, str]]:
    raw = str(getattr(settings, "chat_test_phase_profiles_json", "") or "").strip()
    parsed: Any = {}
    if raw:
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {}

    if not parsed:
        parsed = dict(_DEFAULT_CHAT_TEST_PHASE_PROFILES)

    normalized: dict[str, dict[str, str]] = {}

    if isinstance(parsed, list):
        for row in parsed:
            if not isinstance(row, dict):
                continue
            phase_id = _normalize_phase_identifier(row.get("phase") or row.get("phase_id"))
            if not phase_id:
                continue
            profile = _normalize_chat_test_profile(row)
            if profile:
                normalized[phase_id] = profile
        return normalized

    if not isinstance(parsed, dict):
        return normalized

    for phase_key, profile_payload in parsed.items():
        phase_id = _normalize_phase_identifier(phase_key)
        if not phase_id:
            continue
        profile = _normalize_chat_test_profile(profile_payload)
        if profile:
            normalized[phase_id] = profile

    return normalized


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
    try:
        response = await chat_service.process_message(request, db_session=db)
        response.message = response_beautifier_service.beautify_response_text(response.message)
        if not isinstance(response.metadata, dict):
            response.metadata = {}
        _enrich_service_llm_display_fields(response)
        if trace_id:
            response.metadata.setdefault("trace_id", trace_id)
        response.metadata.setdefault("turn_trace_id", turn_trace_id)
        _record_evaluation_event(request, response, trace_id)
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
        print(f"API DB Error (fallback to in-memory): {e}")
        try:
            response = await chat_service.process_message(request, db_session=None)
            response.message = response_beautifier_service.beautify_response_text(response.message)
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


@router.post("/suggestions")
async def get_contextual_suggestions(request: SuggestionsRequest):
    """
    Fallback suggestion endpoint. Loads live session context (active service,
    conversation state, pending data, phase) and the knowledge base, then asks
    the LLM to generate grounded follow-up suggestions.
    """
    try:
        from services.config_service import config_service

        phase_id = request.current_phase or "pre_booking"
        active_service: dict = {}
        conversation_state: str = ""
        conversation_history: list = []

        # Load live session context when session_id is available
        if request.session_id:
            try:
                context = await context_manager.get_context(request.session_id)
                if context:
                    conversation_state = context.state.value if context.state else ""
                    pending_action = str(context.pending_action or "")

                    # Phase from session context is more reliable than frontend-reported phase
                    ctx_phase = None
                    if context.pending_data:
                        ctx_phase = (
                            context.pending_data.get("phase")
                            or (context.pending_data.get("_integration") or {}).get("phase")
                        )
                    if ctx_phase:
                        phase_id = str(ctx_phase)

                    # Look up the currently active service
                    if pending_action:
                        svc = config_service.get_service(pending_action)
                        if svc:
                            active_service = {
                                "id": svc.get("id", ""),
                                "name": svc.get("name", ""),
                                "profile": svc.get("profile", ""),
                            }

                    # Extract recent conversation history for grounding
                    if hasattr(context, "get_recent_messages"):
                        total_chars = 0
                        for msg in context.get_recent_messages(6):
                            content = str(msg.content or "").strip()
                            if not content or total_chars >= 2000:
                                break
                            clipped = content[: min(2000 - total_chars, 600)]
                            conversation_history.append({"role": msg.role.value, "content": clipped})
                            total_chars += len(clipped)
            except Exception:
                pass  # context load failure is non-fatal

        # Phase description from config — not hardcoded
        phases = config_service.get_journey_phases()
        phase_obj = next((p for p in phases if p.get("id") == phase_id), None)
        phase_name = phase_obj.get("name", phase_id) if phase_obj else phase_id
        phase_description = phase_obj.get("description", "") if phase_obj else ""

        # Services active in this phase from config
        phase_services = config_service.get_phase_services(phase_id)
        available_phase_services = [
            {"name": s.get("name", ""), "profile": s.get("profile", "")}
            for s in phase_services
            if s.get("is_active", True) and s.get("name", "")
        ]

        # Knowledge base text — let the LLM infer topics itself, no hardcoded list
        kb_text = config_service.get_full_kb_text(max_chars=5000)

        # Build bot delivery boundary from live config — fully dynamic per property
        try:
            cap_summary = config_service.get_capability_summary(request.hotel_code or "DEFAULT")
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

        context_payload = {
            "last_bot_message": request.last_bot_message.strip(),
            "user_message": request.user_message.strip() if request.user_message else "",
            "conversation_history": conversation_history,
            "conversation_state": conversation_state,
            "journey_phase": {
                "id": phase_id,
                "name": phase_name,
                "description": phase_description,
            },
            "active_service": active_service,
            "available_phase_services": available_phase_services,
            "knowledge_base_excerpt": kb_text,
            "bot_delivery_boundary": bot_delivery_boundary,
        }

        system_prompt = (
            "You are a suggestion chip generator for a hotel concierge chatbot.\n"
            "Generate 3 suggestions representing what the guest would most likely send next.\n\n"
            "Work through the payload in this order:\n"
            "0. Read `bot_delivery_boundary` first — this defines the hard limits of what this bot can actually deliver. If a suggestion requires the bot to show images or media, provide real-time data, or use a capability not listed in `enabled_capabilities` — discard it. `property_constraints` lists explicit rules for this property that must also be respected.\n"
            "1. Read `last_bot_message` — this is what the bot just said. Suggestions must be a natural direct response to that specific message.\n"
            "2. Read `conversation_history` — understand the thread and do not suggest anything already discussed or resolved.\n"
            "3. Read `active_service` — if a service is in progress, suggest messages that continue or complete that flow.\n"
            "4. Read `knowledge_base_excerpt` to understand what topics the bot can answer as text. If no service is active, only suggest from those topics as relevant to the current phase.\n\n"
            "Voice — strictly enforced:\n"
            "Every suggestion must be a natural first-person guest message, exactly what they would type into the chat.\n"
            "Bad: 'Ask about room types', 'View services', 'Show options', 'Share details'\n"
            "Good: 'What room types do you have?', 'What services are available?', 'Can I see the menu?'\n\n"
            "Never suggest any value that is unique to the individual guest — this includes names, room numbers, phone numbers, email addresses, flight numbers, dates, times, booking references, order quantities, party sizes, prices, or any other personal or context-specific data. The guest is the only one who knows these — they must type them.\n"
            "Also never suggest a message where the guest is offering or providing their personal information, even without stating the actual value. Messages like 'Here's my full name and details', 'I'll share my details', 'Here is my information', 'Let me provide my info' are all forbidden — they imply the guest is about to hand over unique personal data.\n"
            "Suggestions must only be questions the guest wants to ask, or service actions they want to request — never data submissions.\n\n"
            "Each suggestion: 2-8 words. Return ONLY strict JSON: {\"suggestions\": [\"...\", \"...\", \"...\"]}"
        )

        result = await llm_client.chat_with_json(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(context_payload, ensure_ascii=False)},
            ],
            temperature=0.3,
        )
        suggestions = result.get("suggestions", [])
        if not isinstance(suggestions, list):
            suggestions = []
        suggestions = [str(s).strip() for s in suggestions if s and len(str(s).strip()) <= 80][:4]
        return {"suggestions": suggestions}
    except Exception as e:
        print(f"Suggestions endpoint error: {e}")
        return {"suggestions": []}
