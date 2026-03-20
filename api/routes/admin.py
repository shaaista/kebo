"""
Admin API Routes

Endpoints for managing business configuration and settings.
Now uses database-backed storage (MySQL) with JSON fallback.
"""

import asyncio
import time

from fastapi import APIRouter, HTTPException, Request, Depends, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from pathlib import Path
import re
import tempfile
from uuid import uuid4

import hashlib

from services.db_config_service import db_config_service
from services.config_service import config_service  # Keep for JSON fallback
from services.rag_service import rag_service
from services.rag_job_service import rag_job_service
from services.evaluation_metrics_service import evaluation_metrics_service
from services.observability_service import observability_service
from services.gateway_service import gateway_service
from services.menu_ocr_plugin_service import menu_ocr_plugin_service
from services.prompt_writer_service import generate_service_system_prompt
from services.everything_trace_service import everything_trace_service
from config.settings import settings
from llm.client import llm_client

router = APIRouter(prefix="/admin", tags=["Admin"])
templates = Jinja2Templates(directory="templates")

_ADMIN_DB_FAST_FALLBACK_TIMEOUT_SECONDS = max(
    0.2,
    float(getattr(settings, "admin_db_fast_fallback_timeout_seconds", 1.5) or 1.5),
)
_ADMIN_DB_UNAVAILABLE_BACKOFF_SECONDS = max(
    1.0,
    float(getattr(settings, "admin_db_unavailable_backoff_seconds", 15.0) or 15.0),
)
_admin_db_unavailable_until = 0.0


def _admin_db_is_available_now() -> bool:
    return time.monotonic() >= float(_admin_db_unavailable_until or 0.0)


def _mark_admin_db_unavailable() -> None:
    global _admin_db_unavailable_until
    _admin_db_unavailable_until = (
        time.monotonic() + _ADMIN_DB_UNAVAILABLE_BACKOFF_SECONDS
    )


async def _call_db_config_with_fast_fallback(operation: str, coroutine):
    """
    Execute DB config call with fast timeout and temporary backoff.
    This keeps admin UX responsive when DB/VPN is unreachable.
    """
    global _admin_db_unavailable_until
    if not _admin_db_is_available_now():
        if hasattr(coroutine, "close"):
            try:
                coroutine.close()
            except Exception:
                pass
        return False, None
    try:
        result = await asyncio.wait_for(
            coroutine,
            timeout=_ADMIN_DB_FAST_FALLBACK_TIMEOUT_SECONDS,
        )
        _admin_db_unavailable_until = 0.0
        return True, result
    except Exception as error:
        print(f"DB fast-fallback ({operation}) error: {error}")
        _mark_admin_db_unavailable()
        return False, None


# ============ Pydantic Models for Request Validation ============

class UpdateBusinessInfo(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    city: Optional[str] = None
    bot_name: Optional[str] = None
    welcome_message: Optional[str] = None
    timezone: Optional[str] = None
    currency: Optional[str] = None


class UpdateCapability(BaseModel):
    enabled: Optional[bool] = None
    description: Optional[str] = None
    hours: Optional[str] = None


class AddService(BaseModel):
    id: str
    name: str
    type: str
    description: Optional[str] = None
    cuisine: Optional[str] = None
    hours: Optional[dict] = None
    delivery_zones: Optional[list] = None
    is_active: bool = True
    ticketing_plugin_enabled: Optional[bool] = None
    ticketing_cases: Optional[List[Any]] = None
    ticketing_enabled: Optional[bool] = None
    phase_id: Optional[str] = None
    is_builtin: Optional[bool] = None
    service_prompt_pack: Optional[Dict[str, Any]] = None


class AgentPluginSlotInput(BaseModel):
    id: str
    label: Optional[str] = None
    prompt: Optional[str] = None
    required: bool = True


class AgentPluginFactInput(BaseModel):
    id: Optional[str] = None
    text: str
    source: Optional[str] = None
    tags: Optional[List[str]] = None
    status: Optional[str] = None
    approved_by: Optional[str] = None


class UpdateAgentPluginFactInput(BaseModel):
    text: Optional[str] = None
    source: Optional[str] = None
    tags: Optional[List[str]] = None
    status: Optional[str] = None
    approved_by: Optional[str] = None


class AddAgentPlugin(BaseModel):
    id: str
    name: str
    industry: Optional[str] = "custom"
    service_id: Optional[str] = None
    service_category: Optional[str] = "transactional"
    description: Optional[str] = None
    trigger_phrases: Optional[List[str]] = None
    slot_schema: Optional[List[AgentPluginSlotInput]] = None
    confirmation_required: Optional[bool] = True
    channels: Optional[List[str]] = None
    is_active: bool = True
    response_templates: Optional[Dict[str, str]] = None
    knowledge_scope: Optional[Dict[str, Any]] = None
    knowledge_facts: Optional[List[AgentPluginFactInput]] = None
    strict_facts_only: Optional[bool] = True
    tool_bindings: Optional[List[Dict[str, Any]]] = None
    metadata: Optional[Dict[str, Any]] = None
    version: Optional[int] = 1


class UpdateAgentPlugin(BaseModel):
    name: Optional[str] = None
    industry: Optional[str] = None
    service_id: Optional[str] = None
    service_category: Optional[str] = None
    description: Optional[str] = None
    trigger_phrases: Optional[List[str]] = None
    slot_schema: Optional[List[AgentPluginSlotInput]] = None
    confirmation_required: Optional[bool] = None
    channels: Optional[List[str]] = None
    is_active: Optional[bool] = None
    response_templates: Optional[Dict[str, str]] = None
    knowledge_scope: Optional[Dict[str, Any]] = None
    knowledge_facts: Optional[List[AgentPluginFactInput]] = None
    strict_facts_only: Optional[bool] = None
    tool_bindings: Optional[List[Dict[str, Any]]] = None
    metadata: Optional[Dict[str, Any]] = None
    version: Optional[int] = None


class UpdateAgentPluginSettings(BaseModel):
    enabled: Optional[bool] = None
    shared_context: Optional[bool] = None
    strict_mode: Optional[bool] = None
    strict_unavailable_response: Optional[str] = None


class ApproveAgentPluginFactRequest(BaseModel):
    approved_by: Optional[str] = "staff"


class AddFAQEntry(BaseModel):
    id: Optional[str] = None
    question: str
    answer: str
    description: Optional[str] = None
    tags: Optional[List[str]] = None
    enabled: bool = True


class UpdateFAQEntry(BaseModel):
    question: Optional[str] = None
    answer: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[List[str]] = None
    enabled: Optional[bool] = None


class AddToolConfig(BaseModel):
    id: Optional[str] = None
    name: str
    description: Optional[str] = None
    type: Optional[str] = "workflow"
    handler: Optional[str] = None
    channels: Optional[List[str]] = None
    enabled: bool = True
    requires_confirmation: Optional[bool] = False
    ticketing_plugin_enabled: Optional[bool] = None
    ticketing_cases: Optional[List[Any]] = None


class UpdateToolConfig(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    type: Optional[str] = None
    handler: Optional[str] = None
    channels: Optional[List[str]] = None
    enabled: Optional[bool] = None
    requires_confirmation: Optional[bool] = None
    ticketing_plugin_enabled: Optional[bool] = None
    ticketing_cases: Optional[List[Any]] = None



class ApplyTemplate(BaseModel):
    template_name: str
    business_id: str
    business_name: str
    city: str
    bot_name: Optional[str] = "Assistant"


class ImportConfig(BaseModel):
    config_json: str


class UpdateOnboardingBusiness(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None
    type: Optional[str] = None
    city: Optional[str] = None
    location: Optional[str] = None
    address: Optional[str] = None
    timezone: Optional[str] = None
    currency: Optional[str] = None
    language: Optional[str] = None
    timestamp_format: Optional[str] = None
    bot_name: Optional[str] = None
    welcome_message: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    website: Optional[str] = None
    channels: Optional[Dict[str, bool]] = None


class UpdatePromptConfig(BaseModel):
    template_id: Optional[str] = None
    system_prompt: Optional[str] = None
    classifier_prompt: Optional[str] = None
    response_style: Optional[str] = None


class ApplyPromptTemplateRequest(BaseModel):
    template_id: str


class UpdateKnowledgeConfig(BaseModel):
    sources: Optional[List[str]] = None
    notes: Optional[str] = None
    nlu_policy: Optional[Dict[str, Any]] = None


class UpdateUISettings(BaseModel):
    theme: Optional[Dict[str, str]] = None
    widget: Optional[Dict[str, Any]] = None
    channels: Optional[Dict[str, Dict[str, Any]]] = None
    industry_features: Optional[List[str]] = None


class CompileServiceKBRequest(BaseModel):
    service_id: Optional[str] = None
    force: Optional[bool] = False
    max_facts_per_service: Optional[int] = None
    preserve_manual: Optional[bool] = True
    published_by: Optional[str] = "admin"


class UpdateServiceKBManualFactsRequest(BaseModel):
    service_id: str
    plugin_id: Optional[str] = None
    facts: Optional[List[str]] = None
    published_by: Optional[str] = "admin"


class RAGReindexRequest(BaseModel):
    tenant_id: Optional[str] = None
    business_type: Optional[str] = None
    clear_existing: bool = True
    file_paths: Optional[List[str]] = None


class RAGQueryRequest(BaseModel):
    question: str
    tenant_id: Optional[str] = None
    hotel_name: Optional[str] = None
    city: Optional[str] = None
    business_type: Optional[str] = None


class RAGStartJobRequest(BaseModel):
    tenant_id: Optional[str] = None
    business_type: Optional[str] = None
    clear_existing: bool = True
    file_paths: Optional[List[str]] = None


class SuggestServiceDescriptionRequest(BaseModel):
    name: str
    phase_id: Optional[str] = None
    user_intent: Optional[str] = None  # freeform "what do you expect" text from admin


def _normalize_tenant(value: str) -> str:
    return str(value or "default").strip().lower().replace(" ", "_")


def _normalize_identifier(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "_")


def _normalize_phase_identifier(value: Any) -> str:
    from services.config_service import config_service as _cs
    return _cs._normalize_phase_identifier(value)


def _safe_filename(filename: str) -> str:
    name = Path(filename or "upload.txt").name
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", name)
    return safe or f"file_{uuid4().hex[:8]}.txt"


def _fallback_phase_service_description(service_name: str, phase_id: str = "") -> str:
    clean_name = str(service_name or "").strip()
    if not clean_name:
        return "Provide guest support for this service."
    return f"Provide guest support for {clean_name.lower()} requests."


# ============ Business Config API (Database-backed) ============

@router.get("/api/config")
async def get_business_config():
    """Get full business configuration from database."""
    try:
        return await db_config_service.get_full_config()
    except Exception as e:
        # Fallback to JSON
        print(f"DB error, using JSON fallback: {e}")
        return config_service.load_config()


@router.put("/api/config")
async def update_business_config(config: dict):
    """Update full business configuration in database."""
    try:
        if await db_config_service.save_full_config(config):
            return {"message": "Configuration saved to database"}
    except Exception as e:
        print(f"DB error, saving to JSON: {e}")

    # Fallback to JSON
    if config_service.save_config(config):
        return {"message": "Configuration saved to JSON"}
    raise HTTPException(status_code=500, detail="Failed to save configuration")


@router.get("/api/config/business")
async def get_business_info():
    """Get business basic info from database."""
    try:
        return await db_config_service.get_business_info()
    except Exception as e:
        print(f"DB error: {e}")
        return config_service.get_business_info()


@router.put("/api/config/business")
async def update_business_info(update: UpdateBusinessInfo):
    """Update business basic info in database."""
    updates = update.model_dump(exclude_unset=True)

    try:
        result = await db_config_service.update_business_info(updates)
        return result
    except Exception as e:
        print(f"DB error, using JSON: {e}")
        return config_service.update_business_info(updates)


@router.get("/api/config/onboarding/business")
async def get_onboarding_business():
    """
    Step 1 onboarding profile.
    Returns the extended business profile used by admin setup.
    """
    json_business = config_service.get_onboarding_business()
    try:
        db_business = await db_config_service.get_business_info()
        # Keep DB as source for core fields while preserving JSON-only onboarding fields.
        merged = dict(json_business)
        merged.update({k: v for k, v in db_business.items() if v is not None})
        return merged
    except Exception as e:
        print(f"DB error, using JSON onboarding business: {e}")
        return json_business


@router.put("/api/config/onboarding/business")
async def update_onboarding_business(update: UpdateOnboardingBusiness):
    """
    Step 1 onboarding profile update.
    Persists all extended fields in JSON config and core fields in DB when available.
    """
    updates = update.model_dump(exclude_unset=True)
    saved = config_service.update_onboarding_business(updates)

    core_fields = {
        key: value
        for key, value in updates.items()
        if key in {"name", "type", "city", "bot_name", "welcome_message", "timezone", "currency", "language"}
    }
    if core_fields:
        try:
            await db_config_service.update_business_info(core_fields)
        except Exception as e:
            print(f"DB error while syncing onboarding business: {e}")

    return saved


@router.get("/api/config/onboarding/prompts")
async def get_onboarding_prompts():
    """Step 2 onboarding prompt configuration."""
    return config_service.get_prompts()


@router.put("/api/config/onboarding/prompts")
async def update_onboarding_prompts(update: UpdatePromptConfig):
    """Step 2 onboarding prompt configuration update."""
    updates = update.model_dump(exclude_unset=True)
    if config_service.update_prompts(updates):
        return config_service.get_prompts()
    raise HTTPException(status_code=500, detail="Failed to save prompt configuration")


@router.get("/api/config/onboarding/prompt-templates")
async def list_prompt_templates():
    """List available system prompt templates for onboarding."""
    return config_service.list_prompt_templates()


@router.post("/api/config/onboarding/prompts/apply-template")
async def apply_prompt_template(data: ApplyPromptTemplateRequest):
    """Apply a system prompt template and optional NLU defaults."""
    try:
        applied = config_service.apply_prompt_template(data.template_id)
        return {"message": f"Prompt template {data.template_id} applied", "data": applied}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Prompt template not found")


@router.get("/api/config/onboarding/knowledge")
async def get_onboarding_knowledge():
    """Step 3 onboarding: knowledge sources + NLU do/don't rules."""
    return config_service.get_knowledge_config()


@router.put("/api/config/onboarding/knowledge")
async def update_onboarding_knowledge(update: UpdateKnowledgeConfig):
    """Step 3 onboarding update: knowledge sources + NLU do/don't rules."""
    updates = update.model_dump(exclude_unset=True)
    return config_service.update_knowledge_config(updates)


@router.get("/api/config/onboarding/knowledge/conflicts")
async def get_onboarding_knowledge_conflicts():
    """
    Step 3 onboarding validation:
    detect conflicts between configured services and uploaded KB facts.
    """
    return config_service.get_knowledge_conflict_report()


# ============ Evaluation + Observability APIs ============

@router.get("/api/evaluation/summary")
async def get_evaluation_summary(hours: int = 24):
    """Routing/retrieval/policy quality summary for dashboard."""
    window = max(1, min(int(hours), 168))
    return evaluation_metrics_service.get_summary(hours=window)


@router.get("/api/evaluation/events")
async def get_evaluation_events(limit: int = 100):
    """Recent evaluated chat events."""
    capped = max(1, min(int(limit), 500))
    events = evaluation_metrics_service.get_recent_events(limit=capped)
    return {"events": events, "count": len(events)}


@router.get("/api/observability/status")
async def get_observability_status():
    """Runtime diagnostics: logger + gateway controls."""
    return {
        "observability": observability_service.get_status(),
        "gateway": gateway_service.snapshot_state(),
    }


@router.get("/api/observability/events")
async def get_observability_events(limit: int = 100, event: str = ""):
    """Tail observability JSONL events (newest first)."""
    capped = max(1, min(int(limit), 500))
    rows = observability_service.read_recent_events(limit=capped, event_filter=event)
    return {
        "events": rows,
        "count": len(rows),
        "event_filter": str(event or ""),
    }


# ============ RAG Management API ============

@router.get("/api/rag/status")
async def get_rag_status(tenant_id: Optional[str] = None):
    """Get RAG backend/index status for a tenant."""
    business = config_service.get_business_info()
    resolved_tenant = tenant_id or business.get("id") or "default"
    status = rag_service.get_status(tenant_id=resolved_tenant)
    status["backend_mission"] = "tenant-scoped retrieval for web widget + whatsapp"
    status["knowledge_sources_configured"] = config_service.get_knowledge_config().get("sources", [])
    return status


@router.post("/api/rag/reindex")
async def reindex_rag(data: RAGReindexRequest):
    """Ingest/chunk knowledge docs and rebuild tenant-scoped RAG index."""
    business = config_service.get_business_info()
    resolved_tenant = data.tenant_id or business.get("id") or "default"
    resolved_business_type = data.business_type or business.get("type", "generic")

    file_paths = data.file_paths
    if file_paths is None:
        knowledge_sources = config_service.get_knowledge_config().get("sources", [])
        existing_local_paths = []
        for source in knowledge_sources:
            if not isinstance(source, str):
                continue
            path = Path(source)
            if path.exists() and path.is_file():
                existing_local_paths.append(str(path))
        file_paths = existing_local_paths or None

    report = await rag_service.ingest_from_knowledge_base(
        tenant_id=resolved_tenant,
        business_type=resolved_business_type,
        clear_existing=data.clear_existing,
        file_paths=file_paths,
    )
    try:
        config_service.rebuild_structured_kb_library(max_sources=50, save=True)
        await config_service.ensure_structured_kb_llm_books(max_sources=50, force=True)
    except Exception as exc:
        report["library_reindex_warning"] = str(exc)

    try:
        from services.flow_logger import log_reindex
        log_reindex(
            tenant_id=resolved_tenant,
            files=report.get("files") or (file_paths or []),
            chunks_created=int(report.get("chunks_indexed") or 0),
            backend=str(report.get("backend_used") or "local"),
            clear_existing=data.clear_existing,
        )
    except Exception:
        pass

    return report


@router.post("/api/rag/query")
async def debug_rag_query(data: RAGQueryRequest):
    """Debug endpoint to test retrieval + grounded answer generation."""
    question = str(data.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")

    business = config_service.get_business_info()
    resolved_tenant = data.tenant_id or business.get("id") or "default"
    resolved_business_type = data.business_type or business.get("type", "generic")
    resolved_name = data.hotel_name or business.get("name", "Business")
    resolved_city = data.city or business.get("city", "")

    answer = await rag_service.answer_question(
        question=question,
        hotel_name=resolved_name,
        city=resolved_city,
        tenant_id=resolved_tenant,
        business_type=resolved_business_type,
    )
    if answer is None:
        return {
            "handled": False,
            "reason": "no_retrieval_match_or_low_confidence",
            "tenant_id": resolved_tenant,
        }
    return {
        "handled": True,
        "tenant_id": resolved_tenant,
        "answer": answer.answer,
        "confidence": answer.confidence,
        "sources": answer.sources,
    }


@router.post("/api/rag/upload")
async def upload_rag_files(
    files: List[UploadFile] = File(...),
    tenant_id: str = Form(default=""),
    add_to_sources: bool = Form(default=True),
):
    """Upload knowledge files for a tenant. Completely replaces all previous KB files."""
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")

    business = config_service.get_business_info()
    resolved_tenant = tenant_id or business.get("id") or "default"
    normalized_tenant = _normalize_tenant(resolved_tenant)

    # --- WIPE ALL PREVIOUS KB FILES COMPLETELY ---
    # 1. Delete every previously uploaded KB file from disk (entire uploads dir)
    uploads_root = Path(rag_service.kb_dir) / "uploads"
    _files_wiped = 0
    if uploads_root.exists():
        import shutil
        _files_wiped = sum(1 for _ in uploads_root.rglob("*") if _.is_file())
        shutil.rmtree(str(uploads_root), ignore_errors=True)

    # 2. Delete all KB file records from DB
    _db_deleted = 0
    try:
        _db_deleted = await db_config_service.delete_all_kb_files()
    except Exception as _del_err:
        print(f"[DB] KB file delete failed (non-fatal): {_del_err}")

    # 3. Clear all knowledge sources so only the new file will be indexed
    config_service.update_knowledge_config({"sources": []})
    # --- END WIPE ---

    uploads_dir = uploads_root / normalized_tenant
    uploads_dir.mkdir(parents=True, exist_ok=True)

    saved: list[dict[str, str]] = []
    for file in files:
        safe_name = _safe_filename(file.filename or "")
        target_name = f"{uuid4().hex[:8]}_{safe_name}"
        destination = uploads_dir / target_name
        content = await file.read()
        destination.write_bytes(content)
        saved.append(
            {
                "original_name": file.filename or safe_name,
                "saved_name": target_name,
                "path": str(destination.resolve()),
            }
        )
        await file.close()

        # Persist KB content to DB so files survive restarts / machine changes.
        try:
            text_content = content.decode("utf-8", errors="replace")
            content_hash = hashlib.sha256(content).hexdigest()
            await db_config_service.save_kb_file(
                original_name=file.filename or safe_name,
                stored_name=target_name,
                content=text_content,
                content_hash=content_hash,
            )
        except Exception as _kb_err:
            import traceback
            print(f"[DB] KB file save FAILED: {_kb_err}")
            print(traceback.format_exc())

    if add_to_sources:
        # Set sources to ONLY the newly uploaded files (not appended to old list)
        sources = [entry["path"] for entry in saved]
        config_service.update_knowledge_config({"sources": sources})
        asyncio.create_task(config_service.ensure_structured_kb_llm_books(max_sources=50, force=True))

    # Log KB upload event to flow.log
    try:
        from services.flow_logger import log_kb_upload
        for entry in saved:
            raw_content = None
            try:
                raw_content = Path(entry["path"]).read_bytes()
            except Exception:
                pass
            log_kb_upload(
                files_wiped_disk=_files_wiped,
                db_records_deleted=_db_deleted,
                new_file_name=entry["original_name"],
                new_file_bytes=len(raw_content) if raw_content is not None else 0,
                saved_path=entry["path"],
                tenant_id=normalized_tenant,
            )
    except Exception:
        pass

    # Trigger service knowledge refresh after KB upload (non-blocking).
    # This must never fail the upload response path.
    try:
        asyncio.create_task(config_service.enrich_service_kb_records(published_by="system"))
    except Exception as enrich_schedule_error:
        print(f"[KB] Service-KB refresh scheduling failed (non-fatal): {enrich_schedule_error}")

    return {
        "tenant_id": normalized_tenant,
        "uploaded_count": len(saved),
        "files": saved,
        "add_to_sources": add_to_sources,
    }


@router.post("/api/rag/jobs/start")
async def start_rag_index_job(data: RAGStartJobRequest):
    """Start background RAG indexing job."""
    business = config_service.get_business_info()
    resolved_tenant = data.tenant_id or business.get("id") or "default"
    resolved_business_type = data.business_type or business.get("type", "generic")

    file_paths = data.file_paths
    if file_paths is None:
        knowledge_sources = config_service.get_knowledge_config().get("sources", [])
        local_sources: list[str] = []
        for source in knowledge_sources:
            if not isinstance(source, str):
                continue
            path = Path(source)
            if path.exists() and path.is_file():
                local_sources.append(str(path.resolve()))
        file_paths = local_sources

    job = await rag_job_service.start_index_job(
        tenant_id=resolved_tenant,
        business_type=resolved_business_type,
        clear_existing=data.clear_existing,
        file_paths=file_paths,
    )
    return job


@router.get("/api/rag/jobs")
async def list_rag_jobs(limit: int = 20):
    """List recent background RAG indexing jobs."""
    jobs = await rag_job_service.list_jobs(limit=limit)
    return {"jobs": jobs, "count": len(jobs)}


@router.get("/api/rag/jobs/{job_id}")
async def get_rag_job(job_id: str):
    """Get one background indexing job status."""
    job = await rag_job_service.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="RAG job not found")
    return job


@router.get("/api/config/onboarding/ui")
async def get_onboarding_ui():
    """Step 4 onboarding: channel + branding + customization settings."""
    return config_service.get_ui_settings()


@router.put("/api/config/onboarding/ui")
async def update_onboarding_ui(update: UpdateUISettings):
    """Step 4 onboarding update: channel + branding + customization settings."""
    updates = update.model_dump(exclude_unset=True)
    return config_service.update_ui_settings(updates)


@router.get("/api/config/capabilities")
async def get_config_capabilities():
    """Get all capabilities from database."""
    try:
        return await db_config_service.get_capabilities()
    except Exception as e:
        print(f"DB error: {e}")
        return config_service.get_capabilities()


@router.put("/api/config/capabilities/{capability_id}")
async def update_config_capability(capability_id: str, update: UpdateCapability):
    """Update a capability in database."""
    updates = update.model_dump(exclude_unset=True)

    try:
        if await db_config_service.update_capability(capability_id, updates):
            return {"message": f"Capability {capability_id} updated in database"}
    except Exception as e:
        print(f"DB error: {e}")

    if config_service.update_capability(capability_id, updates):
        return {"message": f"Capability {capability_id} updated in JSON"}
    raise HTTPException(status_code=404, detail="Capability not found")


@router.post("/api/config/capabilities")
async def add_config_capability(capability_id: str, data: UpdateCapability):
    """Add a new capability to database."""
    updates = data.model_dump(exclude_unset=True)

    try:
        if await db_config_service.add_capability(capability_id, updates):
            return {"message": f"Capability {capability_id} added"}
    except Exception as e:
        print(f"DB error: {e}")

    raise HTTPException(status_code=500, detail="Failed to add capability")


@router.delete("/api/config/capabilities/{capability_id}")
async def delete_config_capability(capability_id: str):
    """Delete a capability from database."""
    try:
        if await db_config_service.delete_capability(capability_id):
            return {"message": f"Capability {capability_id} deleted"}
    except Exception as e:
        print(f"DB error: {e}")

    raise HTTPException(status_code=500, detail="Failed to delete capability")


async def _generate_and_save_prompt(service_id: str) -> None:
    """Background task: generate an LLM system prompt for a service and persist it."""
    try:
        ok, services = await _call_db_config_with_fast_fallback(
            "get_services",
            db_config_service.get_services(),
        )
        services = services if (ok and isinstance(services, list)) else config_service.get_services()
        sid = str(service_id or "").strip().lower()
        service = next((s for s in (services or []) if str(s.get("id") or "").strip().lower() == sid), None)
        if not service:
            print(f"[PromptWriter] Service '{service_id}' not found after save, skipping generation.")
            return
        prompt = await generate_service_system_prompt(service)
        if prompt:
            db_prompt_ok = await db_config_service.save_generated_prompt(service_id, prompt)
            if not db_prompt_ok:
                # DB save failed (service may only exist in JSON) — persist to JSON directly.
                config_service.update_service(service_id, {"generated_system_prompt": prompt})
                print(f"[PromptWriter] Prompt saved to JSON for '{service_id}' ({len(prompt)} chars)")
            else:
                print(f"[PromptWriter] Prompt saved to DB for '{service_id}' ({len(prompt)} chars)")
            try:
                from services.flow_logger import log_prompt_regen
                pack = service.get("service_prompt_pack") or {}
                extracted_kb = str(
                    service.get("extracted_knowledge")
                    or (pack.get("extracted_knowledge") if isinstance(pack, dict) else "")
                    or ""
                )
                log_prompt_regen(
                    service_id=service_id,
                    service_name=str(service.get("name") or service_id),
                    extracted_kb_chars=len(extracted_kb),
                    generated_prompt_chars=len(prompt),
                )
            except Exception:
                pass
    except Exception as e:
        print(f"[PromptWriter] Background generation failed for '{service_id}': {e}")


async def _log_service_save_snapshot(
    *,
    service_id: str,
    action: str,
    source: str,
    success: bool,
    error: str = "",
) -> None:
    """Write a service persistence snapshot to debugging logs."""
    try:
        sid = str(service_id or "").strip().lower()
        if not sid:
            return
        ok, services = await _call_db_config_with_fast_fallback(
            "get_services",
            db_config_service.get_services(),
        )
        rows = services if (ok and isinstance(services, list)) else config_service.get_services()
        if not isinstance(rows, list):
            rows = []
        svc = next(
            (item for item in rows if str(item.get("id") or "").strip().lower() == sid),
            None,
        )
        pack = (svc or {}).get("service_prompt_pack") or {}
        if not isinstance(pack, dict):
            pack = {}
        from services.flow_logger import log_service_config_save

        log_service_config_save(
            action=str(action or "").strip() or "unknown_action",
            source=str(source or "").strip() or "unknown_source",
            service_id=sid,
            service_name=str((svc or {}).get("name") or sid).strip(),
            description_len=len(str((svc or {}).get("description") or "").strip()),
            ticketing_policy_len=len(str((svc or {}).get("ticketing_policy") or "").strip()),
            extracted_knowledge_len=len(str(pack.get("extracted_knowledge") or "").strip()),
            generated_prompt_len=len(str((svc or {}).get("generated_system_prompt") or "").strip()),
            success=bool(success),
            error=str(error or "").strip(),
        )
    except Exception:
        pass


@router.get("/api/config/services")
async def get_config_services():
    """Get all services from database."""
    ok, db_services = await _call_db_config_with_fast_fallback(
        "get_services",
        db_config_service.get_services(),
    )
    if ok and isinstance(db_services, list):
        return db_services
    return config_service.get_services()


@router.post("/api/config/services")
async def add_config_service(service: AddService):
    """Add a new service to database."""
    payload = service.model_dump()
    ok, db_saved = await _call_db_config_with_fast_fallback(
        "add_service",
        db_config_service.add_service(payload),
    )
    if ok and db_saved:
        asyncio.create_task(_generate_and_save_prompt(payload.get("id")))
        asyncio.create_task(
            _log_service_save_snapshot(
                service_id=str(payload.get("id") or ""),
                action="add_service",
                source="db",
                success=True,
            )
        )
        return {"message": "Service added to database"}

    if config_service.add_service(payload):
        service_id_for_enrichment = payload.get("id")
        asyncio.create_task(config_service.enrich_service_kb_records(
            service_id=service_id_for_enrichment, published_by="system"
        ))
        asyncio.create_task(_generate_and_save_prompt(service_id_for_enrichment))
        asyncio.create_task(
            _log_service_save_snapshot(
                service_id=str(service_id_for_enrichment or ""),
                action="add_service",
                source="json_fallback",
                success=True,
            )
        )
        return {"message": "Service added to JSON"}
    raise HTTPException(status_code=500, detail="Failed to add service")


@router.put("/api/config/services/{service_id}")
async def update_config_service(service_id: str, update: dict):
    """Update a service in database."""
    ok, db_updated = await _call_db_config_with_fast_fallback(
        "update_service",
        db_config_service.update_service(service_id, update),
    )
    if ok and db_updated:
        asyncio.create_task(_generate_and_save_prompt(service_id))
        asyncio.create_task(
            _log_service_save_snapshot(
                service_id=service_id,
                action="update_service",
                source="db",
                success=True,
            )
        )
        return {"message": f"Service {service_id} updated in database"}

    if config_service.update_service(service_id, update):
        asyncio.create_task(config_service.enrich_service_kb_records(
            service_id=service_id, published_by="system"
        ))
        asyncio.create_task(_generate_and_save_prompt(service_id))
        asyncio.create_task(
            _log_service_save_snapshot(
                service_id=service_id,
                action="update_service",
                source="json_fallback",
                success=True,
            )
        )
        return {"message": f"Service {service_id} updated in JSON"}
    raise HTTPException(status_code=404, detail="Service not found")


@router.post("/api/config/services/{service_id}/regenerate-prompt")
async def regenerate_service_prompt(service_id: str):
    """Regenerate the LLM-written system prompt for a service (blocking — returns new prompt)."""
    ok, services = await _call_db_config_with_fast_fallback(
        "get_services",
        db_config_service.get_services(),
    )
    services = services if (ok and isinstance(services, list)) else config_service.get_services()
    sid = str(service_id or "").strip().lower()
    service = next((s for s in (services or []) if str(s.get("id") or "").strip().lower() == sid), None)
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    prompt = await generate_service_system_prompt(service)
    if not prompt:
        raise HTTPException(status_code=500, detail="Prompt generation failed")
    await db_config_service.save_generated_prompt(service_id, prompt)
    try:
        from services.flow_logger import log_prompt_regen
        pack = service.get("service_prompt_pack") or {}
        extracted_kb = str(
            service.get("extracted_knowledge")
            or (pack.get("extracted_knowledge") if isinstance(pack, dict) else "")
            or ""
        )
        log_prompt_regen(
            service_id=service_id,
            service_name=str(service.get("name") or service_id),
            extracted_kb_chars=len(extracted_kb),
            generated_prompt_chars=len(prompt),
        )
    except Exception:
        pass
    return {"generated_system_prompt": prompt}


@router.put("/api/config/services/{service_id}/prompt")
async def save_service_prompt(service_id: str, body: dict):
    """Save a manually edited system prompt for a service."""
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt field is required")
    saved = await db_config_service.save_generated_prompt(service_id, prompt)
    if not saved:
        # DB save failed — fall back to JSON so service prompt is always persisted.
        json_saved = config_service.update_service(service_id, {"generated_system_prompt": prompt})
        if not json_saved:
            await _log_service_save_snapshot(
                service_id=service_id,
                action="manual_prompt_save",
                source="db",
                success=False,
                error="service_not_found_in_db_or_json",
            )
            raise HTTPException(status_code=404, detail="Service not found")
        await _log_service_save_snapshot(
            service_id=service_id,
            action="manual_prompt_save",
            source="json_fallback",
            success=True,
        )
        return {"message": "Prompt saved"}
    await _log_service_save_snapshot(
        service_id=service_id,
        action="manual_prompt_save",
        source="db",
        success=True,
    )
    return {"message": "Prompt saved"}


@router.delete("/api/config/services/clear-all")
async def clear_all_config_services():
    """Delete all services from database and JSON fallback."""
    json_cleared = config_service.clear_services()
    db_config_service.mark_all_services_deleted()

    ok, db_cleared = await _call_db_config_with_fast_fallback(
        "clear_services",
        db_config_service.clear_services(),
    )
    if ok and db_cleared:
        db_config_service.clear_service_delete_tombstones()
        return {"message": "All services deleted from database and JSON"}

    if json_cleared:
        return {"message": "All services deleted from JSON; DB reconciliation queued"}
    return {"message": "Service clear queued for DB reconciliation"}


@router.delete("/api/config/services/{service_id}")
async def delete_config_service(service_id: str):
    """Delete a service from database."""
    normalized_id = _normalize_identifier(service_id)
    if not normalized_id:
        raise HTTPException(status_code=400, detail="Invalid service_id")

    json_deleted = config_service.delete_service(normalized_id)
    db_config_service.mark_service_deleted(normalized_id)

    ok, db_deleted = await _call_db_config_with_fast_fallback(
        "delete_service",
        db_config_service.delete_service(normalized_id),
    )
    if ok and db_deleted:
        db_config_service.unmark_service_deleted(normalized_id)
        return {"message": "Service deleted from database and JSON"}

    if ok and not db_deleted:
        # DB was reachable and the row was already absent.
        db_config_service.unmark_service_deleted(normalized_id)
        if json_deleted:
            return {"message": "Service deleted from JSON; DB already clean"}
        return {"message": "Service already deleted"}

    if json_deleted:
        return {"message": "Service deleted from JSON; DB reconciliation queued"}
    return {"message": "Service delete queued for DB reconciliation"}


@router.get("/api/config/phases")
async def get_config_phases():
    """Get configured journey phases."""
    ok, db_phases = await _call_db_config_with_fast_fallback(
        "get_journey_phases",
        db_config_service.get_journey_phases(),
    )
    if ok and db_phases:
        return db_phases
    return config_service.get_journey_phases()


@router.put("/api/config/phases")
async def update_config_phases(phases: List[Dict[str, Any]]):
    """Replace journey phases."""
    if not isinstance(phases, list):
        raise HTTPException(status_code=400, detail="Phases payload must be a list")
    ok, db_updated = await _call_db_config_with_fast_fallback(
        "update_journey_phases",
        db_config_service.update_journey_phases(phases),
    )
    if ok and db_updated:
        return {"message": "Journey phases updated in database"}

    if config_service.update_journey_phases(phases):
        return {"message": "Journey phases updated in JSON"}
    raise HTTPException(status_code=500, detail="Failed to update journey phases")


@router.get("/api/config/phases/{phase_id}/services")
async def get_config_phase_services(phase_id: str):
    """Get services mapped to one journey phase."""
    normalized_phase_id = _normalize_phase_identifier(phase_id)
    if not normalized_phase_id:
        raise HTTPException(status_code=400, detail="Invalid phase_id")

    ok, db_services = await _call_db_config_with_fast_fallback(
        "get_services_for_phase",
        db_config_service.get_services(),
    )
    services = db_services if ok and isinstance(db_services, list) else config_service.get_services()

    phase_services = [
        dict(service)
        for service in services
        if _normalize_phase_identifier(service.get("phase_id")) == normalized_phase_id
    ]
    return phase_services


@router.get("/api/config/phases/{phase_id}/prebuilt-services")
async def get_config_phase_prebuilt_services(phase_id: str):
    """Get prebuilt service templates for a journey phase."""
    templates = config_service.get_prebuilt_phase_services(phase_id)
    ok, db_services = await _call_db_config_with_fast_fallback(
        "get_services_for_prebuilt_phase",
        db_config_service.get_services(),
    )
    services = db_services if ok and isinstance(db_services, list) else config_service.get_services()

    existing_ids = {
        _normalize_identifier(service.get("id"))
        for service in services
        if isinstance(service, dict)
    }

    enriched: List[Dict[str, Any]] = []
    for template in templates:
        row = dict(template)
        row["is_installed"] = _normalize_identifier(row.get("id")) in existing_ids
        enriched.append(row)
    return enriched


@router.post("/api/config/phases/service-description/suggest")
async def suggest_phase_service_description(payload: SuggestServiceDescriptionRequest):
    """Suggest a short service description from service name."""
    name = str(payload.name or "").strip()
    phase_id = _normalize_phase_identifier(payload.phase_id or "")
    if not name:
        raise HTTPException(status_code=400, detail="Service name is required")

    user_intent = str(payload.user_intent or "").strip()
    fallback = _fallback_phase_service_description(name, phase_id)
    if not str(settings.openai_api_key or "").strip():
        return {"description": fallback, "source": "fallback"}

    prompt_phase = phase_id or "general"
    if user_intent:
        system_msg = (
            "You write clear, professional service descriptions for a hotel chatbot admin panel. "
            "The admin has described what they expect from this service in plain language. "
            "Rewrite it as a crisp 1-2 sentence description suitable for a service catalog. "
            "Keep it factual and specific — no fluff. Return only the description text."
        )
        user_msg = (
            f"Service name: {name}\n"
            f"Journey phase: {prompt_phase}\n"
            f"Admin's expectation: {user_intent}\n\n"
            "Refine this into a clean service description."
        )
        max_tok = 120
    else:
        system_msg = (
            "You write concise admin catalog descriptions for chatbot services. "
            "Return one clear sentence (under 25 words), plain text only. "
            "Be specific to what the service does for hotel guests."
        )
        user_msg = (
            f"Service name: {name}\n"
            f"Journey phase: {prompt_phase}\n"
            "Write a practical description for hotel guest support configuration."
        )
        max_tok = 80
    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_msg},
    ]
    try:
        raw = await llm_client.chat(messages, temperature=0.2, max_tokens=max_tok)
        suggestion = re.sub(r"\s+", " ", str(raw or "").strip()).strip("\"'")
        if not suggestion or "having trouble processing" in suggestion.lower():
            return {"description": fallback, "source": "fallback"}
        if len(suggestion) > 400:
            suggestion = suggestion[:400].rstrip()
        return {"description": suggestion, "source": "llm"}
    except Exception:
        return {"description": fallback, "source": "fallback"}


@router.post("/api/config/phases/ticketing-conditions/suggest")
async def suggest_ticketing_conditions(payload: dict):
    """Suggest or refine ticketing trigger conditions for a service."""
    service_name = str(payload.get("service_name") or "").strip()
    service_description = str(payload.get("service_description") or "").strip()
    current_conditions = str(payload.get("current_conditions") or "").strip()
    if not service_name:
        raise HTTPException(status_code=422, detail="service_name is required")

    if current_conditions:
        system_msg = (
            "You are a hotel chatbot configuration expert. "
            "Refine the given ticketing trigger condition into a clear, precise rule. "
            "1-3 sentences. Be specific about what guest action or data triggers the ticket. "
            "Return only the condition text, nothing else."
        )
        user_msg = (
            f"Service: {service_name}\n"
            f"Description: {service_description}\n"
            f"Current condition to refine: {current_conditions}\n\n"
            "Refine into a clear ticket creation trigger rule."
        )
    else:
        system_msg = (
            "You are a hotel chatbot configuration expert. "
            "Write a clear, specific rule for WHEN the bot should create a backend ticket for a service. "
            "Focus on the key guest confirmation step and required data collected. "
            "1-3 sentences. Return only the condition text, nothing else."
        )
        user_msg = (
            f"Service: {service_name}\n"
            f"Description: {service_description}\n\n"
            "Write a ticket creation trigger condition for this service."
        )
    try:
        raw = await llm_client.chat(
            [{"role": "system", "content": system_msg}, {"role": "user", "content": user_msg}],
            temperature=0.2,
            max_tokens=150,
        )
        conditions = re.sub(r"\s+", " ", str(raw or "").strip()).strip("\"'")
        return {"conditions": conditions or ""}
    except Exception as exc:
        return {"conditions": "", "reason": str(exc)}


@router.get("/api/config/agent-plugins/settings")
async def get_agent_plugin_settings():
    """Get global service-agent plugin settings."""
    return config_service.get_agent_plugin_settings()


@router.put("/api/config/agent-plugins/settings")
async def update_agent_plugin_settings(update: UpdateAgentPluginSettings):
    """Update global service-agent plugin settings."""
    updates = update.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No settings updates provided")
    return config_service.update_agent_plugin_settings(updates)


@router.get("/api/config/agent-plugins")
async def get_agent_plugins(
    active_only: bool = False,
    channel: Optional[str] = None,
    industry: Optional[str] = None,
    service_id: Optional[str] = None,
):
    """List configured service-agent plugins."""
    return config_service.get_agent_plugins(
        active_only=active_only,
        channel=channel,
        industry=industry,
        service_id=service_id,
    )


@router.get("/api/config/agent-plugins/{plugin_id}")
async def get_agent_plugin(plugin_id: str):
    """Get one configured service-agent plugin."""
    plugin = config_service.get_agent_plugin(plugin_id)
    if plugin is None:
        raise HTTPException(status_code=404, detail="Agent plugin not found")
    return plugin


@router.post("/api/config/agent-plugins")
async def add_agent_plugin(plugin: AddAgentPlugin):
    """Add (or upsert) one service-agent plugin."""
    payload = plugin.model_dump(exclude_unset=True)
    if config_service.add_agent_plugin(payload):
        return {"message": "Agent plugin saved"}
    raise HTTPException(status_code=400, detail="Invalid agent plugin payload")


@router.put("/api/config/agent-plugins/{plugin_id}")
async def update_agent_plugin(plugin_id: str, update: UpdateAgentPlugin):
    """Update one service-agent plugin."""
    updates = update.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No plugin updates provided")
    if config_service.update_agent_plugin(plugin_id, updates):
        return {"message": f"Agent plugin {plugin_id} updated"}
    raise HTTPException(status_code=404, detail="Agent plugin not found")


@router.delete("/api/config/agent-plugins/clear-all")
async def clear_agent_plugins():
    """Delete all configured service-agent plugins."""
    if config_service.clear_agent_plugins():
        return {"message": "All agent plugins deleted"}
    raise HTTPException(status_code=500, detail="Failed to clear agent plugins")


@router.delete("/api/config/agent-plugins/{plugin_id}")
async def delete_agent_plugin(plugin_id: str):
    """Delete one service-agent plugin."""
    if config_service.delete_agent_plugin(plugin_id):
        return {"message": f"Agent plugin {plugin_id} deleted"}
    raise HTTPException(status_code=404, detail="Agent plugin not found")


@router.get("/api/config/agent-plugins/{plugin_id}/facts")
async def get_agent_plugin_facts(plugin_id: str, status: Optional[str] = None):
    """List fact entries for one service-agent plugin."""
    plugin = config_service.get_agent_plugin(plugin_id)
    if not plugin:
        raise HTTPException(status_code=404, detail="Agent plugin not found")
    return config_service.get_agent_plugin_facts(plugin_id, status=status)


@router.post("/api/config/agent-plugins/{plugin_id}/facts")
async def add_agent_plugin_fact(plugin_id: str, fact: AgentPluginFactInput):
    """Add one fact entry (pending approval by default) to a plugin."""
    payload = fact.model_dump(exclude_unset=True)
    created = config_service.add_agent_plugin_fact(plugin_id, payload)
    if created:
        return {"message": "Fact added to approval queue", "fact": created}
    raise HTTPException(status_code=400, detail="Invalid fact payload or plugin not found")


@router.put("/api/config/agent-plugins/{plugin_id}/facts/{fact_id}")
async def update_agent_plugin_fact(plugin_id: str, fact_id: str, fact: UpdateAgentPluginFactInput):
    """Update one fact entry on a plugin."""
    updates = fact.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fact updates provided")
    updated = config_service.update_agent_plugin_fact(plugin_id, fact_id, updates)
    if updated:
        return {"message": "Fact updated", "fact": updated}
    raise HTTPException(status_code=404, detail="Fact or plugin not found")


@router.post("/api/config/agent-plugins/{plugin_id}/facts/{fact_id}/approve")
async def approve_agent_plugin_fact(
    plugin_id: str,
    fact_id: str,
    payload: ApproveAgentPluginFactRequest,
):
    """Approve one fact entry so runtime can use it."""
    approved = config_service.approve_agent_plugin_fact(
        plugin_id=plugin_id,
        fact_id=fact_id,
        approved_by=str(payload.approved_by or "staff"),
    )
    if approved:
        return {"message": "Fact approved", "fact": approved}
    raise HTTPException(status_code=404, detail="Fact or plugin not found")


@router.post("/api/config/agent-plugins/{plugin_id}/facts/{fact_id}/reject")
async def reject_agent_plugin_fact(plugin_id: str, fact_id: str):
    """Reject one fact entry so runtime ignores it."""
    rejected = config_service.reject_agent_plugin_fact(plugin_id=plugin_id, fact_id=fact_id)
    if rejected:
        return {"message": "Fact rejected", "fact": rejected}
    raise HTTPException(status_code=404, detail="Fact or plugin not found")


@router.delete("/api/config/agent-plugins/{plugin_id}/facts/{fact_id}")
async def delete_agent_plugin_fact(plugin_id: str, fact_id: str):
    """Delete one fact entry from a plugin."""
    if config_service.delete_agent_plugin_fact(plugin_id=plugin_id, fact_id=fact_id):
        return {"message": "Fact deleted"}
    raise HTTPException(status_code=404, detail="Fact or plugin not found")


@router.get("/api/config/service-kb")
async def get_service_kb_records(
    service_id: Optional[str] = None,
    plugin_id: Optional[str] = None,
    active_only: bool = True,
):
    """List service KB records for a service/plugin."""
    return config_service.get_service_kb_records(
        service_id=service_id,
        plugin_id=plugin_id,
        active_only=active_only,
    )


@router.get("/api/config/service-kb/record")
async def get_service_kb_record(
    service_id: Optional[str] = None,
    plugin_id: Optional[str] = None,
    active_only: bool = True,
):
    """Get one service KB record for a service/plugin."""
    payload = config_service.get_service_kb_record(
        service_id=service_id,
        plugin_id=plugin_id,
        active_only=active_only,
    )
    if not payload:
        raise HTTPException(status_code=404, detail="Service KB record not found")
    return payload


@router.post("/api/config/service-kb/compile")
async def compile_service_kb_records(payload: CompileServiceKBRequest):
    """Compile service knowledge packs from KB + admin config for one/all services."""
    result = config_service.compile_service_kb_records(
        service_id=payload.service_id,
        force=bool(payload.force),
        max_facts_per_service=payload.max_facts_per_service,
        preserve_manual=bool(payload.preserve_manual),
        published_by=str(payload.published_by or "admin"),
    )
    # After compiling facts, also run LLM enrichment
    try:
        enrich_result = await config_service.enrich_service_kb_records(
            service_id=payload.service_id,
            force=bool(payload.force),
            published_by=str(payload.published_by or "admin"),
        )
        result["llm_enrichment"] = enrich_result
    except Exception:
        pass  # Non-blocking
    return result


def _load_kb_sections_as_dict() -> dict[str, str]:
    """
    Parse KB source files into an ordered dict of {section_header: content}.
    Handles the double-wrapped JSON format: {data: "{editable: {key: value}}"}
    Falls back to a single '__raw__' entry for plain-text files.
    """
    import json as _json

    source_paths = config_service._resolve_knowledge_source_paths(max_sources=25)
    sections: dict[str, str] = {}
    for path in source_paths:
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if not raw.strip():
            continue
        editable: dict | None = None
        try:
            outer = _json.loads(raw)
            if isinstance(outer, dict) and "data" in outer:
                inner = _json.loads(outer["data"])
                candidate = inner.get("editable") if isinstance(inner, dict) else None
                if isinstance(candidate, dict):
                    editable = candidate
        except Exception:
            pass
        if editable:
            for k, v in editable.items():
                val_str = str(v).strip() if v is not None else ""
                if val_str:
                    sections[k] = val_str
        else:
            sections["__raw__"] = raw.strip()
    return sections


def _load_kb_sections_for_extraction() -> str:
    """Build a formatted KB string from section dict (used as fallback)."""
    sections = _load_kb_sections_as_dict()
    if not sections:
        return ""
    parts = []
    for k, v in sections.items():
        if k == "__raw__":
            parts.append(v)
        else:
            parts.append(f"=== {k} ===\n{v}")
    return "\n\n".join(parts)


def _pull_kb_log(msg: str) -> None:
    """Write a timestamped line to logs/pull_from_kb_debug.txt — guaranteed to persist even if stdout is suppressed."""
    import datetime as _dt
    try:
        log_path = Path(__file__).resolve().parent.parent.parent / "logs" / "pull_from_kb_debug.txt"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with log_path.open("a", encoding="utf-8") as _f:
            _f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass
    print(f"[PullFromKB] {msg}", flush=True)


@router.post("/api/config/service-kb/preview-extract")
async def preview_extract_service_kb(payload: dict):
    """
    Pull from KB: single dedicated LLM call with the full KB in context.
    Extracts verbatim every piece of information relevant to the given service.
    """
    _pull_kb_log("=== ENDPOINT HIT ===")
    service_name = str(payload.get("service_name") or "").strip()
    service_description = str(payload.get("service_description") or "").strip()
    existing_menu_facts = [
        str(f).strip()
        for f in (payload.get("existing_menu_facts") or [])
        if str(f).strip()
    ]
    _pull_kb_log(f"service_name='{service_name}' desc_chars={len(service_description)}")

    if not service_name:
        raise HTTPException(status_code=422, detail="service_name is required")

    # Load KB with proper section formatting (not the whitespace-collapsed blob)
    kb_text = _load_kb_sections_for_extraction()
    _pull_kb_log(f"_load_kb_sections returned {len(kb_text)} chars")
    if not kb_text.strip():
        # fallback to config_service method
        kb_text = config_service.get_full_kb_text()
        _pull_kb_log(f"fallback get_full_kb_text returned {len(kb_text)} chars")
    if not kb_text.strip():
        _pull_kb_log("no_kb_content — returning early")
        return {"extracted_knowledge": "", "reason": "no_kb_content"}

    _pull_kb_log(f"kb_chars={len(kb_text)} model={settings.openai_model}")

    menu_block = ""
    if existing_menu_facts:
        facts_text = "\n".join(f"- {f}" for f in existing_menu_facts[:300])
        menu_block = (
            f"\n\nThe following facts were already captured from an uploaded menu file. "
            f"Do NOT re-extract or repeat these — only extract KB content NOT already covered here:\n\n"
            f"{facts_text}"
        )

    system_prompt = (
        f"You are a knowledge extractor for a hotel chatbot.\n\n"
        f"SERVICE NAME: {service_name}\n"
        f"SERVICE DESCRIPTION: {service_description}\n\n"
        f"YOUR TASK:\n"
        f"Scan the ENTIRE knowledge base below — every section — and extract every piece of information "
        f"that belongs to or directly supports '{service_name}'. "
        f"Section headers are hints but the content itself determines relevance.\n\n"
        f"STRICT RULES:\n"
        f"1. Copy text EXACTLY word-for-word. No rephrasing, summarising, or paraphrasing.\n"
        f"2. Do NOT omit any relevant detail — prices, timings, policies, item names must all be copied in full.\n"
        f"3. Do NOT invent or add anything not present in the KB.\n"
        f"4. Extract ALL relevant content from ALL sections — even if the same fact appears in multiple sections, "
        f"include it from each section it appears in. Then at the end, remove exact duplicate lines/facts, "
        f"keeping the most complete version of each.\n"
        f"5. Do NOT include content that is clearly about a completely different service "
        f"(e.g. restaurant dining menus when extracting for spa, parking details when extracting for medical).\n"
        f"6. Preserve original structure: section headers, sub-keys, bullet points, values.\n"
        f"7. If the KB contains absolutely nothing relevant to this service, return exactly: NO_RELEVANT_INFO\n"
        f"{menu_block}"
    )

    user_prompt = (
        f"HOTEL KNOWLEDGE BASE:\n\n{kb_text}\n\n"
        f"---\n\n"
        f"Scan the full knowledge base above and extract VERBATIM every piece of information "
        f"relevant to '{service_name}' ({service_description}). "
        f"Copy it exactly as written — every name, price, timing, policy and detail."
    )

    try:
        _t0 = time.perf_counter()
        _pull_kb_log(f"calling LLM model={settings.openai_model} system_chars={len(system_prompt)} user_chars={len(user_prompt)}")
        result = await llm_client.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
            max_tokens=4000,
            trace_context={"actor": "pull_from_kb", "service_name": service_name},
        )
        _dur = round((time.perf_counter() - _t0) * 1000)
        extracted = str(result or "").strip()
        _pull_kb_log(f"LLM raw response preview: {repr(extracted[:200])}")
        if extracted.upper().startswith("NO_RELEVANT_INFO"):
            extracted = ""

        _pull_kb_log(f"LLM done in {_dur}ms extracted_chars={len(extracted)}")
        try:
            from services.flow_logger import log_pull_from_kb
            log_pull_from_kb(
                service_name=service_name,
                service_description=service_description,
                kb_chars=len(kb_text),
                extracted_chars=len(extracted),
                extraction_mode="single_llm",
            )
        except Exception:
            pass

        return {
            "extracted_knowledge": extracted,
            "reason": "ok_llm" if extracted else "no_relevant_info",
            "extraction_mode": "single_llm",
        }
    except Exception as exc:
        import traceback as _tb
        _pull_kb_log(f"EXCEPTION: {exc}\n{_tb.format_exc()}")
        return {"extracted_knowledge": "", "reason": str(exc)}


@router.put("/api/config/service-kb/manual-facts")
async def update_service_kb_manual_facts(payload: UpdateServiceKBManualFactsRequest):
    """
    Replace manual override facts for one service knowledge pack.
    Auto-extracted facts are preserved; manual rows are replaced by provided list.
    """
    record = config_service.set_service_kb_manual_facts(
        service_id=payload.service_id,
        plugin_id=payload.plugin_id,
        facts=list(payload.facts or []),
        published_by=str(payload.published_by or "admin"),
    )
    if not record:
        raise HTTPException(status_code=400, detail="Failed to update service KB manual facts")
    return {"message": "Service KB manual facts updated", "record": record}


@router.get("/api/agent-builder/menu-ocr/status")
async def get_menu_ocr_status():
    """Get standalone menu OCR plugin status."""
    return menu_ocr_plugin_service.get_status()


@router.post("/api/agent-builder/menu-ocr/scan")
async def scan_menu_ocr_for_agent_builder(
    file: UploadFile = File(...),
    service_name: str = Form(default=""),
    max_facts: int = Form(default=100),
):
    """
    Run standalone Menu OCR plugin for one uploaded menu file and return
    one-line fact suggestions.
    """
    safe_name = _safe_filename(file.filename or "menu_upload.pdf")
    try:
        content = await file.read()
    finally:
        await file.close()

    if not content:
        raise HTTPException(status_code=400, detail="Uploaded menu file is empty")

    bounded_max_facts = max(10, min(int(max_facts), 300))
    with tempfile.TemporaryDirectory(prefix="builder_menu_ocr_") as tmp_dir:
        upload_path = Path(tmp_dir) / safe_name
        upload_path.write_bytes(content)
        try:
            ocr_result = menu_ocr_plugin_service.scan_menu(
                upload_path=upload_path,
                menu_name_hint=str(service_name or safe_name),
                max_facts=bounded_max_facts,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    # Pass OCR structured output through LLM for clean plain-text formatting
    raw_ocr_json = str(ocr_result.get("ocr_raw_output_text") or "").strip()
    formatted_text = raw_ocr_json  # fallback if LLM fails
    if raw_ocr_json and str(settings.openai_api_key or "").strip():
        try:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are a menu formatter. You will receive raw JSON output from an OCR pipeline "
                        "that extracted a restaurant/hotel menu. Your job is to reformat it into clean, "
                        "readable plain text — like a well-laid-out menu a human would read.\n\n"
                        "Rules:\n"
                        "- Do NOT add any information that is not present in the input.\n"
                        "- Do NOT remove any information from the input.\n"
                        "- Organise by category/section if categories are present.\n"
                        "- For each item include: name, description (if any), price (if any), dietary tags (veg/non-veg, if any).\n"
                        "- Use clean formatting: section headings, item names, indented details.\n"
                        "- Output plain text only — no markdown, no JSON, no bullet symbols unless they aid readability.\n"
                        "- Preserve all notes, footer text, and allergen info from the input."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Format this OCR menu output into clean plain text:\n\n{raw_ocr_json}",
                },
            ]
            formatted_text = await llm_client.chat(messages, temperature=0.1, max_tokens=4000)
            formatted_text = str(formatted_text or "").strip() or raw_ocr_json
        except Exception:
            formatted_text = raw_ocr_json

    ocr_result["formatted_text"] = formatted_text
    return ocr_result


@router.get("/api/agent-builder/menu-ocr/logs")
async def list_menu_ocr_logs(limit: int = 20):
    """List recent OCR scan logs (latest first)."""
    return {"logs": menu_ocr_plugin_service.list_recent_logs(limit=limit)}


@router.get("/api/agent-builder/menu-ocr/logs/{run_id}")
async def get_menu_ocr_log(run_id: str):
    """Get one OCR scan log by trace/run ID."""
    payload = menu_ocr_plugin_service.get_log(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="OCR log not found")
    return payload


@router.get("/api/config/faq-bank")
async def get_config_faq_bank():
    """Get admin FAQ bank entries."""
    try:
        return await db_config_service.get_faq_bank()
    except Exception as e:
        print(f"DB error: {e}")
        return config_service.get_faq_bank()


@router.post("/api/config/faq-bank")
async def add_config_faq_entry(faq: AddFAQEntry):
    """Add a FAQ bank entry."""
    payload = faq.model_dump(exclude_unset=True)
    try:
        if await db_config_service.add_faq_entry(payload):
            return {"message": "FAQ entry added"}
    except Exception as e:
        print(f"DB error: {e}")

    if config_service.add_faq_entry(payload):
        return {"message": "FAQ entry added to JSON"}
    raise HTTPException(status_code=500, detail="Failed to add FAQ entry")


@router.put("/api/config/faq-bank/{faq_id}")
async def update_config_faq_entry(faq_id: str, update: UpdateFAQEntry):
    """Update a FAQ bank entry."""
    updates = update.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No FAQ updates provided")

    try:
        if await db_config_service.update_faq_entry(faq_id, updates):
            return {"message": f"FAQ entry {faq_id} updated"}
    except Exception as e:
        print(f"DB error: {e}")

    if config_service.update_faq_entry(faq_id, updates):
        return {"message": f"FAQ entry {faq_id} updated in JSON"}
    raise HTTPException(status_code=404, detail="FAQ entry not found")


@router.delete("/api/config/faq-bank/{faq_id}")
async def delete_config_faq_entry(faq_id: str):
    """Delete a FAQ bank entry."""
    try:
        if await db_config_service.delete_faq_entry(faq_id):
            return {"message": f"FAQ entry {faq_id} deleted"}
    except Exception as e:
        print(f"DB error: {e}")

    if config_service.delete_faq_entry(faq_id):
        return {"message": f"FAQ entry {faq_id} deleted from JSON"}
    raise HTTPException(status_code=404, detail="FAQ entry not found")


@router.get("/api/config/tools")
async def get_config_tools():
    """Get admin tools/workflow definitions."""
    try:
        return await db_config_service.get_tools()
    except Exception as e:
        print(f"DB error: {e}")
        return config_service.get_tools()


@router.post("/api/config/tools")
async def add_config_tool(tool: AddToolConfig):
    """Add a tool/workflow entry."""
    payload = tool.model_dump(exclude_unset=True)
    try:
        if await db_config_service.add_tool(payload):
            return {"message": "Tool added"}
    except Exception as e:
        print(f"DB error: {e}")

    if config_service.add_tool(payload):
        return {"message": "Tool added to JSON"}
    raise HTTPException(status_code=500, detail="Failed to add tool")


@router.put("/api/config/tools/{tool_id}")
async def update_config_tool(tool_id: str, update: UpdateToolConfig):
    """Update a tool/workflow entry."""
    updates = update.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No tool updates provided")

    try:
        if await db_config_service.update_tool(tool_id, updates):
            return {"message": f"Tool {tool_id} updated"}
    except Exception as e:
        print(f"DB error: {e}")

    if config_service.update_tool(tool_id, updates):
        return {"message": f"Tool {tool_id} updated in JSON"}
    raise HTTPException(status_code=404, detail="Tool not found")


@router.delete("/api/config/tools/{tool_id}")
async def delete_config_tool(tool_id: str):
    """Delete a tool/workflow entry."""
    try:
        if await db_config_service.delete_tool(tool_id):
            return {"message": f"Tool {tool_id} deleted"}
    except Exception as e:
        print(f"DB error: {e}")

    if config_service.delete_tool(tool_id):
        return {"message": f"Tool {tool_id} deleted from JSON"}
    raise HTTPException(status_code=404, detail="Tool not found")




@router.get("/api/config/escalation")
async def get_escalation_config():
    """Get escalation settings from database."""
    try:
        return await db_config_service.get_escalation_config()
    except Exception as e:
        print(f"DB error: {e}")
        return config_service.get_escalation_config()


@router.put("/api/config/escalation")
async def update_escalation_config(update: dict):
    """Update escalation settings in database."""
    try:
        if await db_config_service.update_escalation_config(update):
            return {"message": "Escalation settings updated in database"}
    except Exception as e:
        print(f"DB error: {e}")

    if config_service.update_escalation_config(update):
        return {"message": "Escalation settings updated in JSON"}
    raise HTTPException(status_code=500, detail="Failed to update")


@router.get("/api/config/templates")
async def list_templates():
    """List available configuration templates."""
    return config_service.list_templates()


@router.post("/api/config/templates/apply")
async def apply_template(data: ApplyTemplate):
    """Apply a template with custom business info."""
    try:
        config = config_service.apply_template(
            data.template_name,
            {
                "id": data.business_id,
                "name": data.business_name,
                "city": data.city,
                "bot_name": data.bot_name,
            }
        )
        # Also save to database
        await db_config_service.save_full_config(config)
        return {"message": f"Template {data.template_name} applied", "config": config}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Template not found")


@router.get("/api/config/export")
async def export_config():
    """Export config as JSON."""
    try:
        config = await db_config_service.get_full_config()
        import json
        return {"config_json": json.dumps(config, indent=2, ensure_ascii=False)}
    except Exception:
        return {"config_json": config_service.export_config()}


@router.post("/api/config/import")
async def import_config(data: ImportConfig):
    """Import config from JSON."""
    import json
    try:
        config = json.loads(data.config_json)
        await db_config_service.save_full_config(config)
        return {"message": "Configuration imported to database"}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    except Exception as e:
        print(f"DB error: {e}")
        if config_service.import_config(data.config_json):
            return {"message": "Configuration imported to JSON"}
        raise HTTPException(status_code=400, detail="Import failed")


# ============ Database Status API ============

@router.get("/api/db/status")
async def get_db_status():
    """Get database connection status and table info."""
    from models.database import engine
    from sqlalchemy import text

    try:
        async with engine.connect() as conn:
            # Test connection
            await conn.execute(text("SELECT 1"))

            # Get table counts
            tables = [
                "new_bot_hotels", "new_bot_restaurants", "new_bot_menu_items",
                "new_bot_guests", "new_bot_orders", "new_bot_order_items",
                "new_bot_conversations", "new_bot_messages",
                "new_bot_business_config", "new_bot_capabilities", "new_bot_intents",
                "new_bot_services", "new_bot_kb_files",
            ]

            counts = {}
            for table in tables:
                try:
                    result = await conn.execute(text(f"SELECT COUNT(*) FROM {table}"))
                    counts[table] = result.scalar()
                except:
                    counts[table] = "table not found"

            return {
                "status": "connected",
                "database": "GHN_PROD_BAK",
                "host": "172.16.5.32",
                "tables": counts
            }
    except Exception as e:
        return {
            "status": "disconnected",
            "error": str(e),
            "note": "Make sure OpenVPN is connected"
        }


@router.post("/api/db/sync")
async def sync_json_to_db():
    """Sync JSON config to database."""
    try:
        config = config_service.load_config()
        await db_config_service.save_full_config(config)
        return {"message": "JSON config synced to database"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sync failed: {e}")


@router.post("/api/db/init")
async def init_database():
    """Initialize/create database tables."""
    from models.database import init_db
    try:
        await init_db()
        return {"message": "Database tables created/verified"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Init failed: {e}")


# ============ Local Tickets API ============

@router.get("/api/tickets")
async def list_local_tickets():
    """Return all locally stored tickets."""
    import json
    from pathlib import Path
    path = Path("./data/ticketing/local_tickets.json")
    if not path.exists():
        return {"tickets": [], "total": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tickets = list(data.get("tickets") or [])
        # newest first
        tickets.sort(key=lambda t: t.get("created_at", ""), reverse=True)
        return {"tickets": tickets, "total": len(tickets)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load tickets: {e}")


# ============ Admin UI ============

@router.get("", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    """Serve the admin dashboard."""
    return templates.TemplateResponse(
        "admin.html",
        {"request": request, "title": "Admin Portal"}
    )
