"""
Chat API Routes

Endpoints for chat functionality and testing.
"""

from fastapi import APIRouter, HTTPException, Depends, Request
from typing import Optional

from schemas.chat import ChatRequest, ChatResponse
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
                "intent": str(getattr(response.intent, "value", response.intent) or ""),
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
    context = await context_manager.get_context(session_id, db_session=db)
    if context is None:
        raise HTTPException(status_code=404, detail="Session not found")

    return {
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


@router.delete("/session/{session_id}")
async def delete_session(session_id: str, db: AsyncSession = Depends(get_db)):
    """
    Delete a session and its history.
    """
    deleted = await context_manager.delete_context(session_id, db_session=db)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"message": "Session deleted", "session_id": session_id}


@router.get("/sessions")
async def list_sessions(db: AsyncSession = Depends(get_db)):
    """
    List all active sessions.
    """
    sessions = await context_manager.list_sessions(db_session=db)
    summaries = []
    for sid in sessions:
        summary = await context_manager.get_conversation_summary(sid, db_session=db)
        summaries.append(summary)
    return {"sessions": summaries, "count": len(summaries)}


@router.post("/session/{session_id}/reset")
async def reset_session(session_id: str, db: AsyncSession = Depends(get_db)):
    """
    Reset a session's state to IDLE without deleting history.
    """
    context = await context_manager.get_context(session_id, db_session=db)
    if context is None:
        raise HTTPException(status_code=404, detail="Session not found")

    from schemas.chat import ConversationState
    context.state = ConversationState.IDLE
    context.pending_action = None
    context.pending_data = {}
    await context_manager.save_context(context, db_session=db)

    return {"message": "Session reset", "state": "idle"}
