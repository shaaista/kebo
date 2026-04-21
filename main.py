"""
NexOria - Main Application Entry Point
"""

# ── Safe stdout/stderr wrapper (Windows Errno 22 prevention) ──────────
# On Windows the console handle can become invalid causing OSError [Errno 22]
# on any print/write to stdout/stderr, which crashes the entire request.
# Wrapping early guarantees every print() and logging write is safe.
import sys as _sys
import io as _io


class _SafeStream:
    """Drop-in stdout/stderr wrapper that silently swallows OSError."""

    def __init__(self, stream):
        self._inner = stream

    def write(self, s):
        try:
            return self._inner.write(s)
        except OSError:
            return len(s)

    def flush(self):
        try:
            self._inner.flush()
        except OSError:
            pass

    def isatty(self):
        try:
            return self._inner.isatty()
        except Exception:
            return False

    @property
    def encoding(self):
        return getattr(self._inner, "encoding", "utf-8")

    def __getattr__(self, name):
        return getattr(self._inner, name)


try:
    if _sys.platform == "win32":
        _sys.stdout = _SafeStream(_sys.stdout)
        _sys.stderr = _SafeStream(_sys.stderr)
except Exception:
    pass
# ── End safe stream wrapper ───────────────────────────────────────────

import asyncio
from time import perf_counter
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
import uvicorn

from config.settings import settings
from api.routes.chat import router as chat_router
from api.routes.admin import router as admin_router
from api.routes.lumira_compat import router as lumira_compat_router
from models.database import init_db
from services.config_service import config_service
from services.db_config_service import db_config_service
from services.gateway_service import gateway_service
from services.observability_service import observability_service
from services.backend_trace_service import backend_trace_service
from services.everything_trace_service import everything_trace_service
from services.step_trace_service import step_trace_service
from services.log_retention_service import log_retention_service
from services.log_setup_service import log_setup_service
from services import new_detailed_logger


def _print_api_log(method: str, path: str, query: str, status_code: int, duration_ms: float) -> None:
    """Emit compact terminal logs for /api and /admin/api calls."""
    path_text = str(path or "")
    is_admin_api = path_text.startswith("/admin/api/")
    is_public_api = path_text.startswith("/api/")
    if not (is_admin_api or is_public_api):
        return
    target = f"{path_text}?{query}" if str(query or "").strip() else path_text
    tag = "[ADMIN_API]" if is_admin_api else "[API]"
    try:
        print(
            f"{tag} {str(method or '').upper()} {target} -> {int(status_code)} ({float(duration_ms):.2f} ms)",
            flush=True,
        )
    except Exception:
        pass


async def _periodic_log_cleanup_task() -> None:
    """Run age-based log cleanup in the background."""
    interval_minutes = max(
        5,
        int(getattr(settings, "log_retention_cleanup_interval_minutes", 360) or 360),
    )
    interval_seconds = float(interval_minutes * 60)
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            summary = log_retention_service.cleanup_old_logs()
            step_trace_service.log_event(
                "log_retention_cleanup",
                payload=summary,
                component="main._periodic_log_cleanup_task",
                step="periodic_cleanup",
                stage="completed",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            step_trace_service.log_event(
                "log_retention_cleanup_failed",
                payload={},
                component="main._periodic_log_cleanup_task",
                step="periodic_cleanup",
                stage="failed",
                error=str(exc),
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    cleanup_task: asyncio.Task | None = None

    # Startup
    from services.flow_logger import ensure_log_files
    ensure_log_files()
    log_setup_summary = log_setup_service.ensure_configured_log_files()
    step_trace_service.log_event(
        "log_files_ensured",
        payload=log_setup_summary,
        component="main.lifespan",
        step="log_bootstrap",
        stage="completed",
    )

    if bool(getattr(settings, "log_retention_cleanup_on_startup", True)):
        cleanup_summary = log_retention_service.cleanup_old_logs()
        step_trace_service.log_event(
            "log_retention_cleanup",
            payload=cleanup_summary,
            component="main.lifespan",
            step="startup_cleanup",
            stage="completed",
        )

    print(f"Starting {settings.app_name}...")
    print(f"Environment: {settings.app_env}")

    # ── new_detailed_logger: startup begin ──────────────────────────────────
    new_detailed_logger.log_startup_begin(
        app_name=str(settings.app_name),
        host=str(settings.host),
        port=int(settings.port),
        env=str(settings.app_env),
    )

    # Initialize database
    db_init_timeout = max(
        5.0,
        float(getattr(settings, "admin_db_fast_fallback_timeout_seconds", 30.0) or 30.0),
    )
    try:
        await asyncio.wait_for(init_db(), timeout=db_init_timeout)
        print("Database initialized")
        new_detailed_logger.log_db_init(success=True)
    except asyncio.TimeoutError:
        timeout_msg = f"Database init timed out after {db_init_timeout:.0f}s"
        print(f"{timeout_msg} (continuing with startup)")
        new_detailed_logger.log_db_init(success=False, error=timeout_msg)
    except Exception as e:
        print(f"Database init failed (will use in-memory): {e}")
        new_detailed_logger.log_db_init(success=False, error=str(e))

    # Restore KB files from DB if missing from disk, then sync services to JSON.
    try:
        from services.rag_service import rag_service as _rag
        restored = await db_config_service.restore_kb_files(str(_rag.kb_dir))
        if restored:
            print(f"Restored {restored} KB file(s) from database")
        await db_config_service.get_services()  # Syncs DB services → JSON
        new_detailed_logger.log_kb_restore(restored=restored or 0)
    except Exception as e:
        print(f"Startup DB sync failed (non-fatal): {e}")
        new_detailed_logger.log_kb_restore(restored=0, error=str(e))

    # Sync prompt markdown files (prompts/defaults/<industry>/*.md) into the
    # DB-backed registry. Safe to run on every startup — hotel overrides are
    # never touched; industry defaults only change when the file hash differs.
    try:
        from services.prompt_seed_service import seed_prompts_from_files
        inserted, updated, unchanged = await seed_prompts_from_files()
        print(
            f"Prompt registry seed: inserted={inserted} updated={updated} unchanged={unchanged}"
        )
    except Exception as e:
        print(f"Prompt registry seed failed (non-fatal): {e}")

    display_host = str(settings.host)
    if display_host in {"0.0.0.0", "::"}:
        display_host = "localhost"
    print(f"Server: http://{display_host}:{settings.port}")
    print(f"API Docs: http://{display_host}:{settings.port}/docs")
    print(f"Test Chat: http://{display_host}:{settings.port}/chat")
    print(f"Admin Portal: http://{display_host}:{settings.port}/admin")

    # ── new_detailed_logger: server ready ───────────────────────────────────
    new_detailed_logger.log_startup_ready(
        host=str(settings.host),
        port=int(settings.port),
    )

    if bool(getattr(settings, "log_retention_enabled", True)):
        cleanup_task = asyncio.create_task(_periodic_log_cleanup_task())

    yield

    # Shutdown
    if cleanup_task is not None:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    print("Shutting down...")
    new_detailed_logger.log_shutdown()


app = FastAPI(
    title=settings.app_name,
    description="Hotel Chatbot API - Conversational AI for guest services",
    version="2.0.0",
    lifespan=lifespan,
)


# ── Global exception handler — catches errors that Starlette's ServerErrorMiddleware
#    would otherwise convert to bare text/plain 500 responses ──────────────────
@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception):
    """Return JSON (never HTML/text) for all unhandled exceptions."""
    import traceback as _tb
    # Write full traceback to file so it's never lost
    try:
        _crash_path = Path(__file__).resolve().parent / "logs" / "gateway_crash.log"
        _crash_path.parent.mkdir(parents=True, exist_ok=True)
        from datetime import datetime as _dt
        with _crash_path.open("a", encoding="utf-8") as _cf:
            _cf.write(
                f"\n[{_dt.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                f"{request.method} {request.url.path} — {exc!r}\n{_tb.format_exc()}\n"
            )
    except Exception:
        pass
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc), "error_type": type(exc).__name__},
    )


@app.middleware("http")
async def gateway_middleware(request: Request, call_next):
    """Gateway hardening: trace-id, auth guard, and simple rate limiting."""
    trace_id = request.headers.get("x-trace-id") or observability_service.new_trace_id()
    request.state.trace_id = trace_id
    start = perf_counter()
    query_string = str(request.url.query or "")

    path = request.url.path or ""
    is_docs_or_public = path in {"/", "/health", "/api/config"} or path.startswith(
        ("/docs", "/openapi.json", "/redoc", "/static")
    )
    # Keep gateway controls on runtime chat traffic. Admin APIs stay open for
    # local operator UX (same behavior as the earlier stable project variant).
    protected_prefix = path.startswith("/api/chat")
    should_guard = protected_prefix and not is_docs_or_public
    should_trace_backend = (
        path.startswith("/api/")
        or path.startswith("/admin/api/")
        or path.startswith("/guest-journey/")
        or path.startswith("/engage-bot/")
    )

    client_host = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )
    step_trace_service.log_event(
        "http_request_start",
        payload={
            "query": dict(request.query_params),
            "client_host": client_host,
            "user_agent": request.headers.get("user-agent", ""),
            "should_guard": should_guard,
            "should_trace_backend": should_trace_backend,
        },
        trace_id=trace_id,
        endpoint=path,
        method=request.method,
        component="main.gateway_middleware",
        step="request_received",
        stage="start",
    )

    # -- new_detailed_logger: HTTP request in --
    try:
        new_detailed_logger.log_http_request(
            trace_id=trace_id,
            method=request.method,
            path=path,
            client_host=client_host,
            user_agent=request.headers.get("user-agent", ""),
            query=dict(request.query_params) or None,
        )
    except Exception as _ndl_exc:
        try:
            import sys as _sys
            print(f"[new_detailed_logger] HTTP request log failed: {_ndl_exc!r}", file=_sys.stderr, flush=True)
        except Exception:
            pass

    if should_trace_backend:
        backend_trace_service.log_event(
            "http_request_start",
            {
                "path": path,
                "query": dict(request.query_params),
                "client_host": client_host,
                "user_agent": request.headers.get("user-agent", ""),
            },
            trace_id=trace_id,
            endpoint=path,
            method=request.method,
            component="main.gateway_middleware",
        )
        everything_trace_service.log_event(
            "http_request_start",
            {
                "path": path,
                "query": dict(request.query_params),
                "client_host": client_host,
                "user_agent": request.headers.get("user-agent", ""),
                "headers": {
                    "x-trace-id": request.headers.get("x-trace-id", ""),
                    "x-forwarded-for": request.headers.get("x-forwarded-for", ""),
                    "referer": request.headers.get("referer", ""),
                    "origin": request.headers.get("origin", ""),
                },
            },
            trace_id=trace_id,
            endpoint=path,
            method=request.method,
            component="main.gateway_middleware",
        )

    if should_guard:
        provided_api_key = request.headers.get("x-api-key")
        if not gateway_service.is_authorized(provided_api_key):
            step_trace_service.log_event(
                "http_request_denied_auth",
                payload={"client_host": client_host},
                trace_id=trace_id,
                endpoint=path,
                method=request.method,
                status_code=401,
                component="main.gateway_middleware",
                step="auth_check",
                stage="failed",
                error="unauthorized_api_key",
            )
            observability_service.log_event(
                "gateway_denied_auth",
                {
                    "trace_id": trace_id,
                    "path": path,
                    "method": request.method,
                },
            )
            # -- new_detailed_logger: rejected - auth --
            try:
                new_detailed_logger.log_http_rejected(
                    trace_id=trace_id,
                    reason="unauthorized_api_key",
                    path=path,
                    method=request.method,
                    client_host=client_host,
                    status_code=401,
                )
            except Exception as _ndl_exc:
                try:
                    import sys as _sys
                    print(f"[new_detailed_logger] rejected log failed: {_ndl_exc!r}", file=_sys.stderr, flush=True)
                except Exception:
                    pass
            if should_trace_backend:
                backend_trace_service.log_event(
                    "http_request_denied_auth",
                    {
                        "path": path,
                        "client_host": client_host,
                    },
                    trace_id=trace_id,
                    endpoint=path,
                    method=request.method,
                    status_code=401,
                    component="main.gateway_middleware",
                    error="unauthorized_api_key",
                )
                everything_trace_service.log_event(
                    "http_request_denied_auth",
                    {
                        "path": path,
                        "client_host": client_host,
                    },
                    trace_id=trace_id,
                    endpoint=path,
                    method=request.method,
                    status_code=401,
                    component="main.gateway_middleware",
                    error="unauthorized_api_key",
                )
            duration_ms = round((perf_counter() - start) * 1000.0, 2)
            _print_api_log(
                method=request.method,
                path=path,
                query=query_string,
                status_code=401,
                duration_ms=duration_ms,
            )
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized: invalid API key.", "trace_id": trace_id},
                headers={"X-Trace-Id": trace_id},
            )

    if should_guard:
        rate_limit_key = f"{client_host}:{path}"
        allowed, retry_after = gateway_service.allow_request(rate_limit_key)
        if not allowed:
            step_trace_service.log_event(
                "http_request_rate_limited",
                payload={
                    "client_host": client_host,
                    "retry_after_seconds": retry_after,
                    "rate_limit_key": rate_limit_key,
                },
                trace_id=trace_id,
                endpoint=path,
                method=request.method,
                status_code=429,
                component="main.gateway_middleware",
                step="rate_limit_check",
                stage="failed",
                error="rate_limited",
            )
            observability_service.log_event(
                "gateway_rate_limited",
                {
                    "trace_id": trace_id,
                    "path": path,
                    "method": request.method,
                    "client_host": client_host,
                    "retry_after_seconds": retry_after,
                },
            )
            # -- new_detailed_logger: rejected - rate limited --
            try:
                new_detailed_logger.log_http_rejected(
                    trace_id=trace_id,
                    reason="rate_limited",
                    path=path,
                    method=request.method,
                    client_host=client_host,
                    status_code=429,
                    extra=f"retry_after={retry_after}s",
                )
            except Exception as _ndl_exc:
                try:
                    import sys as _sys
                    print(f"[new_detailed_logger] rate-limit log failed: {_ndl_exc!r}", file=_sys.stderr, flush=True)
                except Exception:
                    pass
            if should_trace_backend:
                backend_trace_service.log_event(
                    "http_request_rate_limited",
                    {
                        "path": path,
                        "client_host": client_host,
                        "retry_after_seconds": retry_after,
                    },
                    trace_id=trace_id,
                    endpoint=path,
                    method=request.method,
                    status_code=429,
                    component="main.gateway_middleware",
                    error="rate_limited",
                )
                everything_trace_service.log_event(
                    "http_request_rate_limited",
                    {
                        "path": path,
                        "client_host": client_host,
                        "retry_after_seconds": retry_after,
                    },
                    trace_id=trace_id,
                    endpoint=path,
                    method=request.method,
                    status_code=429,
                    component="main.gateway_middleware",
                    error="rate_limited",
                )
            duration_ms = round((perf_counter() - start) * 1000.0, 2)
            _print_api_log(
                method=request.method,
                path=path,
                query=query_string,
                status_code=429,
                duration_ms=duration_ms,
            )
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests.", "retry_after_seconds": retry_after, "trace_id": trace_id},
                headers={
                    "Retry-After": str(retry_after),
                    "X-Trace-Id": trace_id,
                },
            )

    step_trace_service.log_event(
        "http_request_dispatch",
        payload={"client_host": client_host},
        trace_id=trace_id,
        endpoint=path,
        method=request.method,
        component="main.gateway_middleware",
        step="handler_dispatch",
        stage="start",
    )
    try:
        response = await call_next(request)
    except Exception as exc:
        duration_ms = round((perf_counter() - start) * 1000.0, 2)
        step_trace_service.log_event(
            "http_request_exception",
            payload={"duration_ms": duration_ms, "client_host": client_host},
            trace_id=trace_id,
            endpoint=path,
            method=request.method,
            status_code=500,
            component="main.gateway_middleware",
            step="handler_dispatch",
            stage="failed",
            error=str(exc),
        )
        # -- Write full traceback to file so OSError/Errno-22 never hides root cause --
        try:
            import traceback as _tb
            _crash_path = Path(__file__).resolve().parent / "logs" / "gateway_crash.log"
            _crash_path.parent.mkdir(parents=True, exist_ok=True)
            from datetime import datetime as _dt
            with _crash_path.open("a", encoding="utf-8") as _cf:
                _cf.write(
                    f"\n[{_dt.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"{request.method} {path} — {exc!r}\n{_tb.format_exc()}\n"
                )
        except Exception:
            pass
        observability_service.log_event(
            "gateway_unhandled_exception",
            {
                "trace_id": trace_id,
                "path": path,
                "method": request.method,
                "duration_ms": duration_ms,
                "error": str(exc),
            },
        )
        # -- new_detailed_logger: HTTP response (unhandled exception) --
        try:
            new_detailed_logger.log_http_response(
                trace_id=trace_id,
                method=request.method,
                path=path,
                status_code=500,
                duration_ms=duration_ms,
                error=str(exc),
            )
        except Exception as _ndl_exc:
            try:
                import sys as _sys
                print(f"[new_detailed_logger] exception response log failed: {_ndl_exc!r}", file=_sys.stderr, flush=True)
            except Exception:
                pass
        if should_trace_backend:
            backend_trace_service.log_event(
                "http_request_exception",
                {
                    "path": path,
                    "duration_ms": duration_ms,
                    "client_host": client_host,
                },
                trace_id=trace_id,
                endpoint=path,
                method=request.method,
                component="main.gateway_middleware",
                error=str(exc),
            )
            everything_trace_service.log_event(
                "http_request_exception",
                {
                    "path": path,
                    "duration_ms": duration_ms,
                    "client_host": client_host,
                },
                trace_id=trace_id,
                endpoint=path,
                method=request.method,
                component="main.gateway_middleware",
                error=str(exc),
            )
        _print_api_log(
            method=request.method,
            path=path,
            query=query_string,
            status_code=500,
            duration_ms=duration_ms,
        )
        # Return JSON error instead of re-raising (prevents bare 500 HTML responses)
        return JSONResponse(
            status_code=500,
            content={"detail": str(exc), "trace_id": trace_id},
            headers={"X-Trace-Id": trace_id},
        )

    duration_ms = round((perf_counter() - start) * 1000.0, 2)
    response.headers["X-Trace-Id"] = trace_id
    response.headers["X-Response-Time-Ms"] = str(duration_ms)
    step_trace_service.log_event(
        "http_request_end",
        payload={"duration_ms": duration_ms, "client_host": client_host},
        trace_id=trace_id,
        endpoint=path,
        method=request.method,
        status_code=response.status_code,
        component="main.gateway_middleware",
        step="response_emitted",
        stage="completed",
    )

    # -- new_detailed_logger: HTTP response out --
    try:
        new_detailed_logger.log_http_response(
            trace_id=trace_id,
            method=request.method,
            path=path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
    except Exception as _ndl_exc:
        try:
            import sys as _sys
            print(f"[new_detailed_logger] response log failed: {_ndl_exc!r}", file=_sys.stderr, flush=True)
        except Exception:
            pass

    observability_service.log_event(
        "api_request",
        {
            "trace_id": trace_id,
            "path": path,
            "method": request.method,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
            "client_host": client_host,
        },
    )
    if should_trace_backend:
        backend_trace_service.log_event(
            "http_request_end",
            {
                "path": path,
                "duration_ms": duration_ms,
                "client_host": client_host,
            },
            trace_id=trace_id,
            endpoint=path,
            method=request.method,
            status_code=response.status_code,
            component="main.gateway_middleware",
        )
        everything_trace_service.log_event(
            "http_request_end",
            {
                "path": path,
                "duration_ms": duration_ms,
                "client_host": client_host,
            },
            trace_id=trace_id,
            endpoint=path,
            method=request.method,
            status_code=response.status_code,
            component="main.gateway_middleware",
        )
    _print_api_log(
        method=request.method,
        path=path,
        query=query_string,
        status_code=response.status_code,
        duration_ms=duration_ms,
    )
    return response

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files and UI bundles
_BASE_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _BASE_DIR / "static"
_ADMIN_UI_DIST_DIR = (_BASE_DIR / "admin_ui" / "dist").resolve()
_CHAT_UI_FILE = (_ADMIN_UI_DIST_DIR / "chat.html").resolve()

if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
else:
    print(f"Warning: static directory not found: {_STATIC_DIR}")

# Include routers
app.include_router(chat_router)
app.include_router(admin_router)
app.include_router(lumira_compat_router)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Open admin first for setup."""
    return RedirectResponse(url="/admin", status_code=307)


@app.get("/chat", response_class=HTMLResponse)
async def chat_ui():
    """Serve the React chat harness build."""
    if not _CHAT_UI_FILE.exists():
        return HTMLResponse(
            "<h3>NexOria API is running.</h3><p>Chat UI build is unavailable. Build admin_ui with npm run build.</p>",
            status_code=503,
        )
    return FileResponse(_CHAT_UI_FILE)


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "app": settings.app_name,
        "environment": settings.app_env,
    }


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Silence browser favicon requests when no icon file is configured."""
    return Response(status_code=204)


@app.get("/api/config")
async def get_config():
    """Get public configuration for frontend."""
    return {
        "app_name": settings.app_name,
        "max_message_length": 2000,
        "confidence_threshold": settings.intent_confidence_threshold,
    }


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.is_development,
        access_log=True,
        log_level="info",
    )
