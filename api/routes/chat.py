"""
Chat API Routes

Endpoints for chat functionality and testing.
"""

import json
from fastapi import APIRouter, HTTPException, Depends, Request
from typing import Optional, List
from pydantic import BaseModel

from schemas.chat import ChatRequest, ChatResponse
from llm.client import llm_client
from services.chat_service import chat_service
from services.evaluation_metrics_service import evaluation_metrics_service
from services.observability_service import observability_service
from services.conversation_audit_service import conversation_audit_service
from config.settings import settings
from core.context_manager import context_manager
from models.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import OperationalError


router = APIRouter(prefix="/api/chat", tags=["Chat"])


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
    try:
        trace_id = getattr(http_request.state, "trace_id", "")
        response = await chat_service.process_message(request, db_session=db)
        if not isinstance(response.metadata, dict):
            response.metadata = {}
        _enrich_service_llm_display_fields(response)
        if trace_id:
            response.metadata.setdefault("trace_id", trace_id)
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
        return response
    except OperationalError as e:
        print(f"API DB Error (fallback to in-memory): {e}")
        try:
            trace_id = getattr(http_request.state, "trace_id", "")
            response = await chat_service.process_message(request, db_session=None)
            if not isinstance(response.metadata, dict):
                response.metadata = {}
            _enrich_service_llm_display_fields(response)
            response.metadata["db_fallback"] = True
            if trace_id:
                response.metadata.setdefault("trace_id", trace_id)
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
                trace_id=getattr(http_request.state, "trace_id", ""),
                error=str(fallback_error),
                db_fallback=True,
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
            trace_id=getattr(http_request.state, "trace_id", ""),
            error=str(e),
            db_fallback=False,
        )
        raise HTTPException(status_code=500, detail=str(e))


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
