"""
Widget Origin Allowlist Middleware

Enforces per-widget origin allowlists on embed-facing routes:

  - GET /chat                  (the iframe page itself; gated via Referer + widget_key)
  - GET /api/widget/bootstrap  (handled inside the route too, but middleware sets CORS headers)
  - * /api/chat/*              (when invoked from inside an embedded widget, gated via widget_key)

Behavior:
  - If the request has no widget_key, the middleware no-ops (admin/dev paths still work).
  - If widget_key is present and unknown, returns 403 with no body (don't leak existence).
  - If widget_key is present, valid, but origin is not allowed, returns 403.
  - On success, sets Access-Control-Allow-Origin to the request origin (specific, not *)
    and Access-Control-Allow-Credentials: true so iframe sessions work cross-origin.

For /chat, also sets Content-Security-Policy: frame-ancestors limited to the
deployment's allowed_origins, so the iframe page cannot be embedded by random sites.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response, JSONResponse

from services.widget_deployment_service import widget_deployment_service


_GUARDED_PREFIXES = ("/api/chat", "/api/widget/bootstrap")
_CHAT_PATH = "/chat"


def _extract_widget_key(request: Request) -> str:
    """Find widget_key in query, header, or cookie."""
    key = (request.query_params.get("widget_key") or "").strip()
    if key:
        return key
    key = (request.headers.get("x-widget-key") or "").strip()
    if key:
        return key
    key = (request.cookies.get("kebo_widget_key") or "").strip()
    return key


def _request_origin(request: Request) -> str:
    """Prefer Origin header; fall back to Referer's origin."""
    origin = (request.headers.get("origin") or "").strip()
    if origin:
        return origin
    referer = (request.headers.get("referer") or "").strip()
    if not referer:
        return ""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(referer)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        pass
    return ""


def _is_guarded(path: str) -> bool:
    if path == _CHAT_PATH or path.startswith(_CHAT_PATH + "/"):
        return True
    return any(path.startswith(prefix) for prefix in _GUARDED_PREFIXES)


def _build_csp_frame_ancestors(allowed: Iterable[str]) -> str:
    items: List[str] = []
    for origin in allowed:
        if not origin:
            continue
        if origin == "*":
            return "frame-ancestors *"
        items.append(origin)
    if not items:
        # Default: allow self only.
        return "frame-ancestors 'self'"
    items.append("'self'")
    return "frame-ancestors " + " ".join(items)


class WidgetOriginMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path or ""

        if not _is_guarded(path):
            return await call_next(request)

        widget_key = _extract_widget_key(request)

        # No widget_key -> not an embed request; pass through (admin/dev path).
        if not widget_key:
            return await call_next(request)

        deployment = widget_deployment_service.get(widget_key)
        if not deployment or deployment.get("status") != "active":
            return JSONResponse(status_code=403, content={"detail": "widget unavailable"})

        origin = _request_origin(request)

        # CORS preflight: respond directly with allow headers so the browser
        # doesn't have to round-trip a 405 from the actual route.
        if request.method == "OPTIONS":
            if not widget_deployment_service.origin_allowed(widget_key, origin):
                return Response(status_code=403)
            preflight = Response(status_code=204)
            _apply_cors_headers(preflight, origin, deployment)
            return preflight

        if not widget_deployment_service.origin_allowed(widget_key, origin):
            return JSONResponse(status_code=403, content={"detail": "origin not allowed"})

        response = await call_next(request)
        _apply_cors_headers(response, origin, deployment)

        if path == _CHAT_PATH or path.startswith(_CHAT_PATH + "/"):
            response.headers["Content-Security-Policy"] = _build_csp_frame_ancestors(
                deployment.get("allowed_origins") or []
            )

        return response


def _apply_cors_headers(response: Response, origin: str, deployment: dict) -> None:
    """Set per-widget CORS headers. Specific origin (never '*') so credentials work."""
    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Widget-Key, X-Trace-Id"
    response.headers["Access-Control-Expose-Headers"] = "X-Trace-Id, X-Response-Time-Ms"
