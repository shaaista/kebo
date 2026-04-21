"""FastAPI application entry point for Hotel KB Scraper."""

from __future__ import annotations

import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from api.routes.download import router as download_router
from api.routes.scrape import router as scrape_router
from config.settings import settings
from models.job import init_db
from services.job_queue import get_queue_metrics_snapshot, interrupt_stale_running_jobs
from services.metrics import metrics
from services.security import get_client_identifier, has_valid_basic_auth, rate_limiter
from services.worker_supervisor import (
    get_managed_worker_snapshot,
    maybe_start_managed_worker,
    shutdown_managed_worker,
)

LOG_DIR = os.path.join(settings.output_dir, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "hotel-kb-scraper.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-30s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
    force=True,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle startup and shutdown events for the application."""
    await init_db()
    logger.info("Database initialised")
    app.state.asset_version = str(int(time.time()))

    interrupted_jobs = await interrupt_stale_running_jobs()
    if interrupted_jobs:
        logger.info("Interrupted %d stale running job(s) on startup", interrupted_jobs)

    os.makedirs(settings.output_dir, exist_ok=True)
    logger.info("Output directory ready: %s", settings.output_dir)
    try:
        maybe_start_managed_worker()
    except Exception:
        logger.exception("Managed worker auto-start failed during application startup")
    logger.info("Hotel KB Scraper started on port %s", settings.app_port)
    logger.info("File logging enabled: %s", LOG_FILE)

    yield

    shutdown_managed_worker()
    logger.info("Shutting down")


app = FastAPI(
    title="Hotel KB Scraper",
    description="Scrape hotel websites and generate knowledge base files",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _protected_path(path: str) -> bool:
    return path.startswith("/api") or path == "/metrics"


@app.middleware("http")
async def protect_and_log_requests(request: Request, call_next):
    """Apply auth, rate limiting, request logging, and metrics."""
    path = request.url.path
    if not _protected_path(path):
        return await call_next(request)

    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    request.state.request_id = request_id
    client_id = get_client_identifier(request)
    started_at = time.perf_counter()

    logger.info(
        "API request started | request_id=%s | method=%s | path=%s | client=%s",
        request_id,
        request.method,
        path,
        client_id,
    )

    if not has_valid_basic_auth(request.headers.get("Authorization")):
        duration_seconds = time.perf_counter() - started_at
        metrics.record_auth_failure(path=path, client_id=client_id)
        metrics.record_api_request(request.method, path, 401, duration_seconds)
        logger.warning(
            "API auth rejected | request_id=%s | method=%s | path=%s | client=%s",
            request_id,
            request.method,
            path,
            client_id,
        )
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required"},
            headers={
                "WWW-Authenticate": "Basic",
                "X-Request-ID": request_id,
            },
        )

    if path.startswith("/api") and settings.api_rate_limit_enabled:
        allowed = rate_limiter.allow_request(
            client_id,
            limit=settings.api_rate_limit_requests,
            window_seconds=settings.api_rate_limit_window_seconds,
        )
        if not allowed:
            duration_seconds = time.perf_counter() - started_at
            metrics.record_rate_limit_rejection(path=path, client_id=client_id)
            metrics.record_api_request(request.method, path, 429, duration_seconds)
            logger.warning(
                "API rate limit rejected | request_id=%s | method=%s | path=%s | client=%s",
                request_id,
                request.method,
                path,
                client_id,
            )
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Please retry later."},
                headers={
                    "Retry-After": str(settings.api_rate_limit_window_seconds),
                    "X-Request-ID": request_id,
                },
            )

    try:
        response = await call_next(request)
    except Exception:
        duration_seconds = time.perf_counter() - started_at
        metrics.record_api_request(request.method, path, 500, duration_seconds)
        logger.exception(
            "API request crashed | request_id=%s | method=%s | path=%s | duration_ms=%.1f",
            request_id,
            request.method,
            path,
            duration_seconds * 1000,
        )
        raise

    duration_seconds = time.perf_counter() - started_at
    route = request.scope.get("route")
    route_path = getattr(route, "path", path) or path
    metrics.record_api_request(request.method, route_path, response.status_code, duration_seconds)

    log_fn = logger.warning if response.status_code >= 400 else logger.info
    log_fn(
        "API request completed | request_id=%s | method=%s | path=%s | status=%s | duration_ms=%.1f",
        request_id,
        request.method,
        route_path,
        response.status_code,
        duration_seconds * 1000,
    )
    response.headers["X-Request-ID"] = request_id
    return response


app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(scrape_router)
app.include_router(download_router)

templates = Jinja2Templates(directory="templates")


@app.get("/")
async def index(request: Request):
    """Render the main UI page."""
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "version": "1.0.0",
            "asset_version": getattr(app.state, "asset_version", "dev"),
        },
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    """Silence missing favicon requests in local development."""
    return Response(status_code=204)


@app.get("/health")
async def health_check():
    """Simple health check endpoint for monitoring."""
    queue_snapshot = await get_queue_metrics_snapshot()
    return {
        "status": "ok",
        "app": "Hotel KB Scraper",
        "queue": queue_snapshot,
        "worker": get_managed_worker_snapshot(),
    }


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics_endpoint() -> PlainTextResponse:
    """Expose Prometheus-compatible metrics."""
    queue_snapshot = await get_queue_metrics_snapshot()
    metrics.record_stale_workers(queue_snapshot.get("stale_running", 0))
    payload = metrics.render_prometheus(queue_snapshot)
    return PlainTextResponse(payload, media_type="text/plain; version=0.0.4; charset=utf-8")


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.app_port,
        reload=settings.app_env == "development",
    )
