"""
KePSLA Bot v2 - Main Application Entry Point
"""

from time import perf_counter
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import uvicorn

from config.settings import settings
from api.routes.chat import router as chat_router
from api.routes.admin import router as admin_router
from api.routes.lumira_compat import router as lumira_compat_router
from models.database import init_db
from services.config_service import config_service
from services.gateway_service import gateway_service
from services.observability_service import observability_service


async def _run_startup_kb_enrichment() -> None:
    """
    On startup, run LLM-based service knowledge enrichment for any services
    whose extracted_knowledge is stale or missing. Fingerprint-cached so it
    only re-runs when KB content or service definitions actually change.
    Runs silently — failures are non-fatal.
    """
    try:
        if not str(settings.openai_api_key or "").strip():
            print("⚠️  KB enrichment skipped: no OpenAI API key configured")
            return
        kb_text = config_service.get_full_kb_text(max_chars=1000)
        if not kb_text.strip():
            print("ℹ️  KB enrichment skipped: no knowledge base content found")
            return
        print("🧠 Running service KB enrichment pipeline...")
        result = await config_service.enrich_service_kb_records(published_by="system")
        enriched = result.get("enriched_count", 0)
        skipped = result.get("skipped_count", 0)
        if enriched:
            print(f"✅ KB enrichment complete: {enriched} service(s) enriched, {skipped} unchanged")
        else:
            print(f"ℹ️  KB enrichment: all {skipped} service(s) already up to date")
    except Exception as e:
        print(f"⚠️  KB enrichment failed (non-fatal): {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Startup
    print(f"🚀 Starting {settings.app_name}...")
    print(f"📍 Environment: {settings.app_env}")

    # Initialize database
    try:
        await init_db()
        print("✅ Database initialized")
    except Exception as e:
        print(f"⚠️  Database init failed (will use in-memory): {e}")

    # Run LLM-based service KB enrichment in background — does not block startup
    import asyncio
    asyncio.create_task(_run_startup_kb_enrichment())

    print(f"🌐 Server: http://{settings.host}:{settings.port}")
    print(f"📚 API Docs: http://localhost:{settings.port}/docs")
    print(f"💬 Test Chat: http://localhost:{settings.port}/")
    print(f"⚙️  Admin Portal: http://localhost:{settings.port}/admin")
    yield
    # Shutdown
    print("👋 Shutting down...")


app = FastAPI(
    title=settings.app_name,
    description="Hotel Chatbot API - Conversational AI for guest services",
    version="2.0.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def gateway_middleware(request: Request, call_next):
    """Gateway hardening: trace-id, auth guard, and simple rate limiting."""
    trace_id = request.headers.get("x-trace-id") or observability_service.new_trace_id()
    request.state.trace_id = trace_id
    start = perf_counter()

    path = request.url.path or ""
    is_docs_or_public = path in {"/", "/health", "/api/config"} or path.startswith(
        ("/docs", "/openapi.json", "/redoc", "/static")
    )
    # Keep gateway controls on runtime chat traffic. Admin APIs stay open for
    # local operator UX (same behavior as the earlier stable project variant).
    protected_prefix = path.startswith("/api/chat")
    should_guard = protected_prefix and not is_docs_or_public

    if should_guard:
        provided_api_key = request.headers.get("x-api-key")
        if not gateway_service.is_authorized(provided_api_key):
            observability_service.log_event(
                "gateway_denied_auth",
                {
                    "trace_id": trace_id,
                    "path": path,
                    "method": request.method,
                },
            )
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized: invalid API key.", "trace_id": trace_id},
                headers={"X-Trace-Id": trace_id},
            )

    client_host = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )
    if should_guard:
        rate_limit_key = f"{client_host}:{path}"
        allowed, retry_after = gateway_service.allow_request(rate_limit_key)
        if not allowed:
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
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests.", "retry_after_seconds": retry_after, "trace_id": trace_id},
                headers={
                    "Retry-After": str(retry_after),
                    "X-Trace-Id": trace_id,
                },
            )

    try:
        response = await call_next(request)
    except Exception as exc:
        duration_ms = round((perf_counter() - start) * 1000.0, 2)
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
        raise

    duration_ms = round((perf_counter() - start) * 1000.0, 2)
    response.headers["X-Trace-Id"] = trace_id
    response.headers["X-Response-Time-Ms"] = str(duration_ms)
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
    return response

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files and templates
_BASE_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _BASE_DIR / "static"
_TEMPLATES_DIR = _BASE_DIR / "templates"
_CHAT_TEMPLATE_FILE = _TEMPLATES_DIR / "chat.html"

if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
else:
    print(f"Warning: static directory not found: {_STATIC_DIR}")

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Include routers
app.include_router(chat_router)
app.include_router(admin_router)
app.include_router(lumira_compat_router)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Open admin first for setup."""
    return RedirectResponse(url="/admin", status_code=307)


@app.get("/chat", response_class=HTMLResponse)
async def chat_ui(request: Request):
    """Serve the test chat interface."""
    if not _CHAT_TEMPLATE_FILE.exists():
        return HTMLResponse(
            "<h3>KePSLA Bot API is running.</h3><p>Chat UI template is unavailable in this deployment bundle.</p>"
        )
    return templates.TemplateResponse(
        "chat.html",
        {"request": request, "app_name": settings.app_name},
    )


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "app": settings.app_name,
        "environment": settings.app_env,
    }


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
    )
