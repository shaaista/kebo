"""
Lumira compatibility API routes.

These routes preserve Lumira request/response contracts while delegating all
business logic to the existing chat_service pipeline.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import get_db
from services.chat_service import chat_service
from services.response_beautifier_service import response_beautifier_service
from services.lumira_compat_adapter import (
    build_engage_chat_request,
    build_engage_response,
    build_guest_journey_chat_request,
    build_guest_journey_response,
)

router = APIRouter(tags=["Lumira Compatibility"])


def _normalize_error(exc: Exception) -> str:
    detail = str(exc).strip()
    return detail or exc.__class__.__name__


@router.post("/guest-journey/message")
async def guest_journey_message(
    payload: dict[str, Any],
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Lumira-compatible guest-journey endpoint.
    """
    try:
        chat_request = build_guest_journey_chat_request(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.errors()) from exc

    try:
        chat_response = await chat_service.process_message(chat_request, db_session=db)
    except OperationalError:
        chat_response = await chat_service.process_message(chat_request, db_session=None)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_normalize_error(exc)) from exc
    chat_response.message = response_beautifier_service.beautify_response_text(chat_response.message)

    return build_guest_journey_response(chat_response, source_payload=payload)


@router.post("/engage-bot/message")
async def engage_message(
    payload: dict[str, Any],
    session_id: str | None = Header(default=None, alias="session_id"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Lumira-compatible engage endpoint.
    """
    body = payload if isinstance(payload, dict) else {}
    if body.get("entity_id") in (None, "") and body.get("entityId") in (None, "") and body.get("group_id") in (None, "") and body.get("groupId") in (None, ""):
        raise HTTPException(
            status_code=400,
            detail="Missing 'entity_id' or 'group_id' in request body",
        )

    try:
        chat_request = build_engage_chat_request(body, session_id_header=session_id or "")
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.errors()) from exc

    try:
        chat_response = await chat_service.process_message(chat_request, db_session=db)
    except OperationalError:
        chat_response = await chat_service.process_message(chat_request, db_session=None)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_normalize_error(exc)) from exc
    chat_response.message = response_beautifier_service.beautify_response_text(chat_response.message)

    return build_engage_response(
        chat_response,
        source_payload=body,
        session_id=chat_request.session_id,
    )
