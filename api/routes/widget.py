"""
Widget API Routes

Public endpoints used by the embeddable loader, plus admin endpoints for
managing widget deployments. All admin endpoints live under /admin/api/widget/*
so they sit alongside the rest of the admin surface; the public bootstrap lives
under /api/widget/bootstrap.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from services.widget_deployment_service import widget_deployment_service


public_router = APIRouter(prefix="/api/widget", tags=["Widget Public"])
admin_router = APIRouter(prefix="/admin/api/widget", tags=["Widget Admin"])


# ---- Pydantic models -----------------------------------------------------


class ThemeIn(BaseModel):
    brand_color: Optional[str] = None
    accent_color: Optional[str] = None
    bg_color: Optional[str] = None
    text_color: Optional[str] = None


class SizeIn(BaseModel):
    width: Optional[int] = None
    height: Optional[int] = None


class CreateDeployment(BaseModel):
    hotel_code: str
    name: Optional[str] = None
    allowed_origins: Optional[List[str]] = None
    theme: Optional[ThemeIn] = None
    size: Optional[SizeIn] = None
    position: Optional[str] = None
    bot_name: Optional[str] = None
    phase: Optional[str] = None
    auto_open: Optional[bool] = None
    status: Optional[str] = None


class UpdateDeployment(BaseModel):
    hotel_code: Optional[str] = None
    name: Optional[str] = None
    allowed_origins: Optional[List[str]] = None
    theme: Optional[ThemeIn] = None
    size: Optional[SizeIn] = None
    position: Optional[str] = None
    bot_name: Optional[str] = None
    phase: Optional[str] = None
    auto_open: Optional[bool] = None
    status: Optional[str] = None


# ---- Public bootstrap ----------------------------------------------------


def _host_origin(request: Request) -> str:
    """Build the origin the loader should use for the iframe URL."""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    if not host:
        return f"{proto}://{request.url.hostname}"
    return f"{proto}://{host}"


@public_router.get("/bootstrap")
async def bootstrap(request: Request, widget_key: Optional[str] = None, hotel_code: Optional[str] = None):
    """
    Public, unauthenticated bootstrap endpoint consumed by the loader.

    The response only contains embed-safe fields (no secrets, no PII). The loader
    uses it to build the iframe URL and theme the launcher button.
    """
    host = _host_origin(request)
    origin = (request.headers.get("origin") or request.headers.get("referer") or "").strip()

    # Path 1: widget_key supplied — primary, production path.
    if widget_key:
        payload = widget_deployment_service.bootstrap_payload(widget_key, host_origin=host)
        if not payload:
            raise HTTPException(status_code=404, detail="Unknown or inactive widget_key")
        if not widget_deployment_service.origin_allowed(widget_key, origin):
            # Soft-fail: loader will gracefully not render. Return 403 so the
            # browser surfaces the issue in devtools without leaking config.
            raise HTTPException(status_code=403, detail="Origin not allowed for this widget")
        return payload

    # Path 2: hotel_code supplied — MVP fallback so existing snippets keep working.
    if hotel_code:
        defaults = widget_deployment_service._hotel_defaults(hotel_code.strip().lower())
        return {
            "widget_key": None,
            "hotel_code": hotel_code.strip().lower(),
            "bot_name": defaults["bot_name"],
            "phase": defaults["phase"],
            "theme": defaults["theme"],
            "size": defaults["size"],
            "position": defaults["position"],
            "auto_open": defaults["auto_open"],
            "iframe_url": f"{host}/chat",
        }

    raise HTTPException(status_code=400, detail="widget_key or hotel_code required")


# ---- Admin CRUD ----------------------------------------------------------


@admin_router.get("/deployments")
async def list_deployments(hotel_code: Optional[str] = None):
    return {"deployments": widget_deployment_service.list(hotel_code=hotel_code)}


@admin_router.get("/deployments/{widget_key}")
async def get_deployment(widget_key: str):
    dep = widget_deployment_service.get(widget_key)
    if not dep:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return dep


@admin_router.post("/deployments")
async def create_deployment(body: CreateDeployment):
    payload: Dict[str, Any] = body.model_dump(exclude_none=True)
    if not payload.get("hotel_code"):
        raise HTTPException(status_code=400, detail="hotel_code is required")
    return widget_deployment_service.create(payload["hotel_code"], payload)


@admin_router.put("/deployments/{widget_key}")
async def update_deployment(widget_key: str, body: UpdateDeployment):
    payload: Dict[str, Any] = body.model_dump(exclude_none=True)
    updated = widget_deployment_service.update(widget_key, payload)
    if not updated:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return updated


@admin_router.delete("/deployments/{widget_key}")
async def delete_deployment(widget_key: str):
    ok = widget_deployment_service.delete(widget_key)
    if not ok:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return {"ok": True}


@admin_router.post("/deployments/{widget_key}/rotate-key")
async def rotate_key(widget_key: str):
    rotated = widget_deployment_service.rotate_key(widget_key)
    if not rotated:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return rotated
