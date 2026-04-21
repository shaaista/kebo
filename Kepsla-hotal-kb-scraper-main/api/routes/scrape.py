"""API endpoints for crawl, review, and publish workflows."""

from __future__ import annotations

import asyncio
import copy
import gzip
import json
import logging
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from config.settings import settings
from generator.file_packager import build_job_bundle_name, package_all_properties, safe_filename
from models.job import JobStatus, ScrapeJob, async_session
from processor.llm_structurer import structure_property_kb
from processor.review_manifest import build_review_data, build_review_entity
from scraper.content_extractor import extract_content
from scraper.image_downloader import download_property_images
from scraper.orchestrator import expand_property_pages_for_kb, run_discovery_phase
from services.job_state import reconcile_completed_job
from services.job_queue import (
    QUEUE_QUEUED,
    QUEUE_RETRY_WAIT,
    QUEUE_RUNNING,
    QUEUE_STOPPED,
    enqueue_job_task,
    resume_job_task,
    stop_job_task,
)
from services.worker_supervisor import maybe_start_managed_worker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")
_JOB_UPDATE_LOCKS: dict[str, asyncio.Lock] = {}

_REVIEW_READY_STATUSES = {
    JobStatus.PROPERTIES_DETECTED.value,
    JobStatus.STOPPED.value,
    JobStatus.COMPLETED.value,
    JobStatus.FAILED.value,
}

_PUBLISH_ACTIVE_STATUSES = {
    JobStatus.EXTRACTING.value,
    JobStatus.GENERATING.value,
    JobStatus.DOWNLOADING_IMAGES.value,
}

_REVIEW_CONTEXT_VERSION = 2
_ENTITY_PAGE_STORE_INLINE_KEY = "entity_page_store"
_ENTITY_PAGE_STORE_PATH_KEY = "entity_page_store_path"
_ENTITY_PAGE_STORE_FILENAME = "entity_page_store.json.gz"


class ScrapeRequest(BaseModel):
    """Request body for starting a new crawl job."""

    url: str
    project_name: str = "Grand Hotel Bot"
    bot_enabled: bool = True
    language: str = "English"
    specific_urls: list[str] = Field(default_factory=list)
    auto_sync: bool = False
    # Controls what gets included in the final ZIP package:
    # "both"       – KB text + images (default)
    # "kb_only"    – KB text only, skip image downloads
    # "media_only" – images only, skip KB generation
    output_mode: str = "both"


class PublishRequest(BaseModel):
    """Request body for publishing reviewed content."""

    review_data: dict[str, Any] = Field(default_factory=dict)


class ProcessRequest(BaseModel):
    """Legacy request body for selecting properties directly."""

    selected_properties: list[str]


class ScrapeResponse(BaseModel):
    """Immediate response after launching a background job."""

    job_id: str
    status: str
    message: str


class PropertyInfo(BaseModel):
    """Legacy property summary response."""

    name: str
    page_count: int
    sample_urls: list[str]
    sample_titles: list[str]


class PropertiesResponse(BaseModel):
    """Legacy property summary wrapper."""

    job_id: str
    status: str
    total_properties: int
    properties: list[PropertyInfo]


class JobStatusResponse(BaseModel):
    """Full status payload for a job."""

    job_id: str
    status: str
    queue_state: str = ""
    task_type: str = ""
    progress_pct: int
    progress_msg: str
    pages_found: int
    pages_crawled: int
    pages_failed: int
    properties_found: int
    kb_preview: str
    error_message: str
    output_dir: str = ""
    session_name: str = ""
    can_stop: bool = False
    can_resume: bool = False
    can_open_review: bool = False
    can_download: bool = False
    created_at: str
    completed_at: str | None


class JobSummary(BaseModel):
    """Abbreviated status payload for recent jobs."""

    job_id: str
    session_name: str
    url: str
    status: str
    queue_state: str = ""
    task_type: str = ""
    progress_pct: int
    progress_msg: str
    can_stop: bool = False
    can_resume: bool = False
    can_open_review: bool = False
    can_download: bool = False
    output_dir: str = ""
    created_at: str
    completed_at: str | None


def _parse_json_field(job_id: str, payload: str | None, field_name: str) -> dict[str, Any]:
    """Parse JSON stored in the database safely."""
    if not payload:
        return {}

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        logger.error("Invalid %s JSON for job %s: %s", field_name, job_id, exc, exc_info=True)
        return {}

    if not isinstance(parsed, dict):
        logger.warning(
            "Unexpected %s type for job %s: %s",
            field_name,
            job_id,
            type(parsed).__name__,
        )
        return {}

    return parsed


def _parse_properties_data(job_id: str, properties_data: str | None) -> dict[str, Any]:
    """Parse legacy property summary metadata."""
    return _parse_json_field(job_id, properties_data, "properties_data")


def _parse_review_data(job_id: str, review_data: str | None) -> dict[str, Any]:
    """Parse review-manifest metadata."""
    return _parse_json_field(job_id, review_data, "review_data")


def _parse_job_context_data(job_id: str, job_context_data: str | None) -> dict[str, Any]:
    """Parse persisted background-job context."""
    return _parse_json_field(job_id, job_context_data, "job_context_data")


def _session_name_for_job(job: ScrapeJob) -> str:
    """Derive a readable session name for the homepage session list."""
    review_payload = _parse_review_data(job.id, job.review_data)
    project = review_payload.get("project") or {}
    project_name = (project.get("name") or "").strip()
    if project_name and project_name.lower() != "grand hotel bot":
        return project_name

    job_context = _parse_job_context_data(job.id, job.job_context_data)
    project_settings = job_context.get("project_settings") or {}
    context_name = (project_settings.get("name") or "").strip()
    if context_name and context_name.lower() != "grand hotel bot":
        return context_name

    return _derive_job_label(review_payload, job.url)


def _can_open_review(job: ScrapeJob) -> bool:
    """Return True when the review manager can be reopened for a job."""
    return bool((job.review_data or "").strip())


def _can_stop(job: ScrapeJob) -> bool:
    """Return True when a background task is currently active or queued."""
    return job.queue_state in {QUEUE_QUEUED, QUEUE_RUNNING, QUEUE_RETRY_WAIT}


def _can_resume(job: ScrapeJob) -> bool:
    """Return True when a stopped session still has background work to resume."""
    return job.queue_state == QUEUE_STOPPED and bool((job.task_type or "").strip())


def _can_download(job: ScrapeJob) -> bool:
    """Return True when a completed session should expose download actions."""
    return job.status == JobStatus.COMPLETED.value


def _derive_job_label(review_payload: dict[str, Any], url: str) -> str:
    """Choose a human-readable bundle name for generated output."""
    project = review_payload.get("project") or {}
    project_name = (project.get("name") or "").strip()
    if project_name and project_name.lower() != "grand hotel bot":
        return project_name

    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if host:
        root_label = host.split(".")[0].strip()
        if root_label:
            return root_label

    for entity in review_payload.get("entities", []):
        entity_name = (entity.get("name") or "").strip()
        if entity_name:
            return entity_name

    return "hotel_kb"


def _job_output_root(job_id: str) -> Path:
    """Return the per-job output root directory."""
    return (Path(settings.output_dir) / job_id).resolve()


def _entity_page_store_cache_path(job_id: str) -> Path:
    """Return the canonical cache file path for persisted page-store payloads."""
    return _job_output_root(job_id) / "cache" / _ENTITY_PAGE_STORE_FILENAME


def _persist_entity_page_store(job_id: str, entity_page_store: dict[str, Any]) -> str:
    """Persist heavy page-store payload to disk and return a relative cache path."""
    cache_path = _entity_page_store_cache_path(job_id)
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = cache_path.with_suffix(f"{cache_path.suffix}.tmp")
    with gzip.open(tmp_path, "wt", encoding="utf-8") as handle:
        json.dump(entity_page_store, handle, ensure_ascii=False)
    tmp_path.replace(cache_path)

    return cache_path.relative_to(_job_output_root(job_id)).as_posix()


def _resolve_job_cache_path(job_id: str, stored_path: str) -> Path:
    """Resolve a stored cache path to an absolute filesystem path."""
    candidate = Path(stored_path)
    if candidate.is_absolute():
        return candidate
    return (_job_output_root(job_id) / candidate).resolve()


def _load_entity_page_store(job_id: str, job_context: dict[str, Any]) -> dict[str, Any]:
    """Load entity page-store from inline context (legacy) or disk cache (current)."""
    inline_store = job_context.get(_ENTITY_PAGE_STORE_INLINE_KEY)
    if isinstance(inline_store, dict):
        return inline_store

    stored_path = str(job_context.get(_ENTITY_PAGE_STORE_PATH_KEY, "")).strip()
    if not stored_path:
        return {}

    cache_path = _resolve_job_cache_path(job_id, stored_path)
    if not cache_path.exists():
        logger.warning("Page-store cache missing for job %s: %s", job_id, cache_path)
        return {}

    try:
        if cache_path.suffix.lower() == ".gz":
            with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
                parsed = json.load(handle)
        else:
            with cache_path.open("r", encoding="utf-8") as handle:
                parsed = json.load(handle)
    except Exception as exc:
        logger.error(
            "Failed loading page-store cache for job %s (%s): %s",
            job_id,
            cache_path,
            exc,
            exc_info=True,
        )
        return {}

    if not isinstance(parsed, dict):
        logger.warning("Unexpected page-store cache type for job %s: %s", job_id, type(parsed).__name__)
        return {}

    return parsed


def _remove_job_artifacts(job_id: str, output_dir: str, job_context: dict[str, Any]) -> None:
    """Delete job output/cache files inside configured output_dir safely."""
    output_root = Path(settings.output_dir).resolve()
    candidates: list[Path] = [_job_output_root(job_id)]

    if output_dir.strip():
        candidates.append(Path(output_dir))

    cached_path = str(job_context.get(_ENTITY_PAGE_STORE_PATH_KEY, "")).strip()
    if cached_path:
        candidates.append(_resolve_job_cache_path(job_id, cached_path))

    seen: set[str] = set()
    deduped_candidates: list[Path] = []
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        deduped_candidates.append(candidate)

    for candidate in sorted(deduped_candidates, key=lambda path: len(path.parts), reverse=True):
        resolved = candidate.resolve()
        try:
            resolved.relative_to(output_root)
        except ValueError:
            logger.warning("Skipped deleting path outside output root: %s", resolved)
            continue

        if not resolved.exists():
            continue

        if resolved.is_dir():
            shutil.rmtree(resolved, ignore_errors=True)
        else:
            resolved.unlink(missing_ok=True)


def _stage_progress(start_pct: int, end_pct: int, completed: int, total: int) -> int:
    """Map stage completion to an overall job progress percentage."""
    if total <= 0:
        return end_pct

    ratio = min(max(completed, 0), total) / total
    return start_pct + int((end_pct - start_pct) * ratio)


async def _run_property_stage(
    properties: list[dict[str, Any]],
    *,
    max_parallel: int,
    worker: Callable[[dict[str, Any]], Awaitable[Any]],
    progress_callback: Callable[[int, int, dict[str, Any], Any], Awaitable[None]] | None = None,
) -> list[Any]:
    """Run a bounded-concurrency property stage while preserving input order."""
    total = len(properties)
    if total == 0:
        return []

    semaphore = asyncio.Semaphore(max(1, max_parallel))
    results: list[Any | None] = [None] * total
    completed = 0
    progress_lock = asyncio.Lock()

    async def _run(index: int, property_data: dict[str, Any]) -> None:
        nonlocal completed

        async with semaphore:
            result = await worker(property_data)

        results[index] = result
        async with progress_lock:
            completed += 1

            if progress_callback:
                await progress_callback(completed, total, property_data, result)

    await asyncio.gather(
        *[_run(index, property_data) for index, property_data in enumerate(properties)]
    )
    return [result for result in results if result is not None]


async def _download_selected_property_images(
    *,
    job_id: str,
    properties_to_publish: list[dict[str, Any]],
    job_label: str,
    extracted_at: datetime,
    progress_callback: Callable[[int, int, dict[str, Any], dict[str, Any]], Awaitable[None]] | None = None,
) -> Path:
    """Download images for the selected properties into the named bundle folder."""
    bundle_name = build_job_bundle_name(job_label, extracted_at)
    bundle_output_dir = Path(settings.output_dir) / job_id / bundle_name
    bundle_output_dir.mkdir(parents=True, exist_ok=True)

    async def _download_property_images(property_data: dict[str, Any]) -> dict[str, Any]:
        property_name = property_data.get("name", "Property")
        property_output_dir = bundle_output_dir / safe_filename(property_name)
        image_result = await download_property_images(
            property_name=property_name,
            extracted_pages=property_data.get("extracted_content", []),
            output_dir=property_output_dir,
            max_concurrent=10,
        )
        property_data["images"] = image_result
        return property_data

    await _run_property_stage(
        properties_to_publish,
        max_parallel=settings.max_concurrent_property_image_downloads,
        worker=_download_property_images,
        progress_callback=progress_callback,
    )

    return bundle_output_dir


async def _expand_publish_property_pages(
    property_data: dict[str, Any],
) -> dict[str, Any]:
    """Deep-crawl selected pages while preserving original reviewed coverage.

    Expansion improves coverage when additional relevant pages are discoverable.
    But expansion can also under-fetch due transient crawl/network issues.
    To keep publish output deterministic and avoid regressions, we never drop
    originally reviewed pages/extractions when expansion returns a subset.
    """
    property_name = property_data.get("name", "Property")
    seed_pages = property_data.get("pages", []) or []

    if not seed_pages:
        return property_data

    expanded_pages = await expand_property_pages_for_kb(
        property_name,
        seed_pages,
        max_depth=2,
        max_pages=32,
    )
    if not expanded_pages:
        return property_data

    original_extracted = property_data.get("extracted_content", []) or []
    seed_pages_by_url = {
        page.get("url", ""): page
        for page in seed_pages
        if page.get("url")
    }
    extracted_by_url = {
        page.get("source_url", ""): page
        for page in original_extracted
        if page.get("source_url")
    }

    merged_pages: list[dict[str, Any]] = []
    rebuilt_extracted: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for page in expanded_pages:
        page_url = page.get("url", "")
        if not page_url or page_url in seen_urls:
            continue
        seen_urls.add(page_url)

        # Prefer original reviewed page metadata when available.
        merged_pages.append(copy.deepcopy(seed_pages_by_url.get(page_url, page)))

        if page_url in extracted_by_url:
            rebuilt_extracted.append(copy.deepcopy(extracted_by_url[page_url]))
            continue

        try:
            extracted = extract_content(page.get("html", ""), page_url)
            extracted["source_url"] = page_url
            extracted["crawl_method"] = page.get("method", "unknown")
        except Exception as exc:
            logger.warning("Expanded extraction failed for %s: %s", page_url, exc)
            extracted = _build_extraction_fallback(page, exc)

        rebuilt_extracted.append(extracted)

    # Preserve any original reviewed pages that expansion did not return.
    for page in seed_pages:
        page_url = page.get("url", "")
        if not page_url or page_url in seen_urls:
            continue
        seen_urls.add(page_url)
        merged_pages.append(copy.deepcopy(page))

        original_extracted_page = extracted_by_url.get(page_url)
        if original_extracted_page is not None:
            rebuilt_extracted.append(copy.deepcopy(original_extracted_page))
            continue

        try:
            extracted = extract_content(page.get("html", ""), page_url)
            extracted["source_url"] = page_url
            extracted["crawl_method"] = page.get("method", "unknown")
        except Exception as exc:
            logger.warning("Seed-page extraction fallback failed for %s: %s", page_url, exc)
            extracted = _build_extraction_fallback(page, exc)

        rebuilt_extracted.append(extracted)

    updated_property = copy.deepcopy(property_data)
    updated_property["pages"] = merged_pages
    updated_property["extracted_content"] = rebuilt_extracted
    return updated_property


def _normalise_selected_properties(selected_properties: list[str]) -> list[str]:
    """Strip blanks and deduplicate selections while preserving order."""
    seen: set[str] = set()
    normalised: list[str] = []

    for raw_name in selected_properties:
        name = raw_name.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        normalised.append(name)

    return normalised


def _normalise_specific_urls(urls: list[str]) -> list[str]:
    """Clean user-provided specific URLs while preserving order."""
    seen: set[str] = set()
    cleaned: list[str] = []

    for raw_url in urls:
        url = raw_url.strip()
        if not url or url in seen:
            continue
        if not url.startswith(("http://", "https://")):
            continue
        seen.add(url)
        cleaned.append(url)

    return cleaned


def _build_extraction_fallback(page: dict, exc: Exception) -> dict[str, Any]:
    """Build a minimal extracted-content payload when extraction fails."""
    return {
        "source_url": page.get("url", ""),
        "error": str(exc),
        "title": page.get("title", ""),
        "main_text": page.get("text", ""),
        "markdown": "",
        "tables": [],
        "images": [],
        "videos": [],
        "files": [],
        "contacts": {"phones": [], "emails": [], "address": ""},
        "structured_data": [],
        "meta": {},
        "crawl_method": page.get("method", "unknown"),
    }


def _ensure_background_worker_ready() -> None:
    """Start a managed worker when enabled so queued jobs do not stall."""
    try:
        maybe_start_managed_worker()
    except Exception as exc:
        logger.exception("Background worker unavailable")
        raise HTTPException(
            status_code=503,
            detail="Background worker is unavailable. Check logs and retry.",
        ) from exc


def _build_review_context(
    *,
    job_id: str,
    source_url: str,
    project_settings: dict[str, Any],
    property_groups: dict[str, list[dict]],
) -> tuple[dict[str, Any], dict[str, dict[str, dict[str, Any]]], dict[str, Any]]:
    """Extract review-ready content and build the UI manifest."""
    entities: list[dict[str, Any]] = []
    entity_page_store: dict[str, dict[str, dict[str, Any]]] = {}
    properties_summary: dict[str, Any] = {}

    for entity_index, (property_name, property_pages) in enumerate(property_groups.items(), start=1):
        extracted_pages: list[dict[str, Any]] = []

        for page in property_pages:
            try:
                extracted = extract_content(page.get("html", ""), page.get("url", ""))
                extracted["source_url"] = page.get("url", "")
                extracted["crawl_method"] = page.get("method", "unknown")
            except Exception as exc:
                logger.warning("Review extraction failed for %s: %s", page.get("url", ""), exc)
                extracted = _build_extraction_fallback(page, exc)

            extracted_pages.append(extracted)

        entity, page_store = build_review_entity(
            property_name,
            property_pages,
            extracted_pages,
            entity_index=entity_index,
        )
        entities.append(entity)
        entity_page_store[entity["id"]] = page_store

        properties_summary[property_name] = {
            "page_count": len(property_pages),
            "sample_urls": [page.get("url", "") for page in property_pages[:5]],
            "sample_titles": [page.get("title", "") for page in property_pages[:5] if page.get("title")],
        }

    review_payload = build_review_data(
        job_id=job_id,
        source_url=source_url,
        project=project_settings,
        entities=entities,
    )

    return review_payload, entity_page_store, properties_summary


def _build_publish_queue(review_payload: dict[str, Any], entity_page_store: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert reviewed UI data back into property payloads for KB generation."""
    properties_to_publish: list[dict[str, Any]] = []

    for entity in review_payload.get("entities", []):
        if not entity.get("enabled", True):
            continue

        entity_id = entity.get("id")
        if not entity_id or entity_id not in entity_page_store:
            continue

        page_store = entity_page_store[entity_id]
        page_entries: list[tuple[dict[str, Any], dict[str, Any], str]] = []
        page_payloads_by_id: dict[str, dict[str, Any]] = {}

        for item in entity.get("items", []):
            if item.get("type") != "page" or not item.get("enabled", True):
                continue

            stored = page_store.get(item.get("id", ""))
            if not stored:
                continue

            page = copy.deepcopy(stored["page"])
            extracted = copy.deepcopy(stored["extracted_content"])
            extracted["manual_entity_name"] = (entity.get("name") or entity.get("suggested_name") or "").strip()
            extracted["manual_name"] = (item.get("name") or item.get("suggested_name") or "").strip()
            extracted["manual_label"] = (item.get("label") or item.get("suggested_label") or "").strip()
            extracted["_selected_assets"] = []

            page_entries.append((page, extracted, item["id"]))
            page_payloads_by_id[item["id"]] = extracted

        for item in entity.get("items", []):
            if item.get("type") == "page" or not item.get("enabled", True):
                continue

            source_page_item_id = item.get("source_page_item_id")
            if not source_page_item_id or source_page_item_id not in page_payloads_by_id:
                continue

            page_payloads_by_id[source_page_item_id].setdefault("_selected_assets", []).append(
                {
                    "type": item.get("type", "asset"),
                    "name": (item.get("name") or item.get("suggested_name") or "").strip(),
                    "label": (item.get("label") or item.get("suggested_label") or "").strip(),
                    "url": item.get("url", ""),
                }
            )

        if not page_entries:
            continue

        properties_to_publish.append(
            {
                "name": (entity.get("name") or entity.get("suggested_name") or "Property").strip(),
                "pages": [page for page, _, _ in page_entries],
                "extracted_content": [extracted for _, extracted, _ in page_entries],
            }
        )

    return properties_to_publish


async def update_job(
    job_id: str,
    status: str | None = None,
    progress_pct: int | None = None,
    progress_msg: str | None = None,
    **kwargs: Any,
) -> None:
    """Persist job progress to the database."""
    job_lock = _JOB_UPDATE_LOCKS.setdefault(job_id, asyncio.Lock())
    max_attempts = 5

    async with job_lock:
        for attempt in range(1, max_attempts + 1):
            try:
                async with async_session() as session:
                    job = await session.get(ScrapeJob, job_id)
                    if not job:
                        logger.warning("Job update skipped, job not found: %s", job_id)
                        return

                    if (
                        job.queue_state == QUEUE_STOPPED
                        and job.status == JobStatus.STOPPED.value
                        and status != JobStatus.STOPPED.value
                    ):
                        logger.info("Job update ignored because session is stopped: %s", job_id)
                        return

                    previous_status = job.status

                    if status:
                        job.status = status
                    if progress_pct is not None:
                        job.progress_pct = progress_pct
                    if progress_msg:
                        job.progress_msg = progress_msg

                    for key, value in kwargs.items():
                        if hasattr(job, key):
                            setattr(job, key, value)

                    await session.commit()

                    if status and status != previous_status:
                        logger.info(
                            "Job status transition | job_id=%s | from=%s | to=%s | progress=%s | msg=%s",
                            job_id,
                            previous_status,
                            status,
                            job.progress_pct,
                            job.progress_msg,
                        )

                    error_message = kwargs.get("error_message")
                    if error_message:
                        logger.error(
                            "Job error persisted | job_id=%s | status=%s | error=%s",
                            job_id,
                            job.status,
                            error_message,
                        )
                    return
            except OperationalError as exc:
                if "database is locked" not in str(exc).lower() or attempt >= max_attempts:
                    raise

                delay_seconds = 0.2 * attempt
                logger.warning(
                    "SQLite lock while updating job %s (attempt %d/%d). Retrying in %.1fs.",
                    job_id,
                    attempt,
                    max_attempts,
                    delay_seconds,
                )
                await asyncio.sleep(delay_seconds)


async def _run_phase1(job_id: str, url: str, project_settings: dict[str, Any]) -> None:
    """Execute discovery, crawl, extraction, and review-manifest preparation."""
    try:
        logger.info("Phase 1 started for job %s: %s", job_id, url)
        await update_job(
            job_id,
            status=JobStatus.DISCOVERING.value,
            progress_pct=2,
            progress_msg="Starting URL discovery...",
        )

        result = await run_discovery_phase(
            job_id=job_id,
            url=url,
            specific_urls=project_settings.get("specific_urls") or [],
            update_callback=update_job,
        )

        if result.get("error") or not result.get("properties"):
            error_msg = result.get("error", "No properties found after crawling")
            await update_job(
                job_id,
                status=JobStatus.FAILED.value,
                progress_pct=0,
                progress_msg=error_msg,
                error_message=error_msg,
            )
            logger.error("Phase 1 failed for job %s: %s", job_id, error_msg)
            return

        await update_job(
            job_id,
            status=JobStatus.EXTRACTING.value,
            progress_pct=58,
            progress_msg="Preparing reviewable content...",
            properties_found=len(result["properties"]),
            pages_found=result.get("total_pages", 0),
            pages_crawled=result.get("total_pages", 0) - result.get("failed_pages", 0),
            pages_failed=result.get("failed_pages", 0),
        )

        review_payload, entity_page_store, properties_summary = _build_review_context(
            job_id=job_id,
            source_url=url,
            project_settings=project_settings,
            property_groups=result["crawl_data"],
        )
        entity_page_store_path = _persist_entity_page_store(job_id, entity_page_store)

        job_context_payload = {
            "source_url": url,
            "project_settings": project_settings,
            "review_context_version": _REVIEW_CONTEXT_VERSION,
            _ENTITY_PAGE_STORE_PATH_KEY: entity_page_store_path,
        }

        await update_job(
            job_id,
            status=JobStatus.PROPERTIES_DETECTED.value,
            progress_pct=72,
            progress_msg="Review extracted content before publishing.",
            properties_found=len(review_payload.get("entities", [])),
            properties_data=json.dumps(properties_summary, ensure_ascii=False),
            review_data=json.dumps(review_payload, ensure_ascii=False),
            job_context_data=json.dumps(job_context_payload, ensure_ascii=False),
        )

        logger.info(
            "Phase 1 complete for job %s: %d review entities ready",
            job_id,
            len(review_payload.get("entities", [])),
        )
    except Exception as exc:
        error_msg = f"Phase 1 error: {exc}"
        logger.error("Phase 1 failed for job %s: %s", job_id, exc, exc_info=True)
        await update_job(
            job_id,
            status=JobStatus.FAILED.value,
            progress_pct=0,
            progress_msg=error_msg,
            error_message=error_msg,
        )


async def _run_publish(job_id: str, url: str, review_payload: dict[str, Any]) -> None:
    """Generate KB output from reviewed content selections."""
    try:
        async with async_session() as session:
            job = await session.get(ScrapeJob, job_id)

        if job is None:
            raise RuntimeError("The requested job no longer exists.")

        job_context = _parse_job_context_data(job_id, job.job_context_data)
        if not job_context:
            raise RuntimeError("Review context expired. Please re-run the crawl.")

        # Determine output mode from the original job settings
        project_settings = job_context.get("project_settings") or {}
        output_mode = project_settings.get("output_mode", "both")
        if output_mode not in ("both", "kb_only", "media_only"):
            output_mode = "both"

        entity_page_store = _load_entity_page_store(job_id, job_context)
        if not entity_page_store:
            raise RuntimeError("Review context payload is missing. Please re-run the crawl.")

        properties_to_publish = _build_publish_queue(review_payload, entity_page_store)
        if not properties_to_publish:
            raise RuntimeError("No enabled page content was selected for publishing.")

        logger.info(
            "Publish started | job_id=%s | properties=%d",
            job_id,
            len(properties_to_publish),
        )

        await update_job(
            job_id,
            status=JobStatus.EXTRACTING.value,
            progress_pct=72,
            progress_msg=f"Expanding selected property pages for {len(properties_to_publish)} properties...",
            review_data=json.dumps(review_payload, ensure_ascii=False),
            completed_at=None,
            output_dir="",
            error_message="",
            kb_preview="",
        )

        async def _update_expand_progress(
            completed: int,
            total: int,
            _property_data: dict[str, Any],
            expanded_property: dict[str, Any],
        ) -> None:
            expanded_page_count = len(expanded_property.get("pages", []) or [])
            await update_job(
                job_id,
                progress_pct=_stage_progress(72, 77, completed, total),
                progress_msg=(
                    f"Expanded property pages for {completed}/{total} properties "
                    f"({expanded_page_count} pages for {expanded_property.get('name', 'Property')})"
                ),
            )

        properties_to_publish = await _run_property_stage(
            properties_to_publish,
            max_parallel=settings.max_concurrent_property_expansions,
            worker=_expand_publish_property_pages,
            progress_callback=_update_expand_progress,
        )

        await update_job(
            job_id,
            status=JobStatus.GENERATING.value,
            progress_pct=78,
            progress_msg=f"Structuring reviewed content for {len(properties_to_publish)} properties...",
        )

        total_properties = len(properties_to_publish)

        async def _generate_property_kb(property_data: dict[str, Any]) -> dict[str, Any]:
            property_name = property_data.get("name", "Unknown Hotel")
            if output_mode == "media_only":
                # Skip LLM KB generation entirely
                return {
                    "name": property_name,
                    "kb_text": "",
                    "offer_kb_text": "",
                    "extracted_content": copy.deepcopy(property_data.get("extracted_content", [])),
                }
            try:
                kb_text = await structure_property_kb(property_data)
            except Exception as exc:
                logger.error("LLM structuring failed for '%s' (job=%s): %s", property_name, job_id, exc)
                kb_text = ""
                property_data["offer_kb_text"] = ""

            return {
                "name": property_name,
                "kb_text": kb_text,
                "offer_kb_text": property_data.get("offer_kb_text", ""),
                "extracted_content": copy.deepcopy(property_data.get("extracted_content", [])),
            }

        async def _update_generation_progress(
            completed: int,
            total: int,
            _property_data: dict[str, Any],
            generated_property: dict[str, Any],
        ) -> None:
            await update_job(
                job_id,
                progress_pct=_stage_progress(78, 93, completed, total),
                progress_msg=(
                    f"Generated AI knowledge base for {completed}/{total} properties "
                    f"({generated_property.get('name', 'Property')})"
                ),
            )

        properties_with_kb = await _run_property_stage(
            properties_to_publish,
            max_parallel=settings.max_concurrent_kb_generations,
            worker=_generate_property_kb,
            progress_callback=_update_generation_progress,
        )

        first_kb = next((item["kb_text"] for item in properties_with_kb if item["kb_text"]), "")
        if first_kb:
            await update_job(job_id, kb_preview=first_kb[:2000])

        job_label = _derive_job_label(review_payload, url)
        extracted_at = datetime.now(timezone.utc)

        await update_job(
            job_id,
            status=JobStatus.DOWNLOADING_IMAGES.value,
            progress_pct=94,
            progress_msg=f"Downloading images for {total_properties} selected properties...",
        )

        async def _update_image_progress(
            completed: int,
            total: int,
            property_data: dict[str, Any],
            _result: dict[str, Any],
        ) -> None:
            image_stats = property_data.get("images", {}) or {}
            await update_job(
                job_id,
                progress_pct=_stage_progress(94, 97, completed, total),
                progress_msg=(
                    f"Downloaded images for {completed}/{total} properties "
                    f"({image_stats.get('downloaded', 0)} files for {property_data.get('name', 'Property')})"
                ),
            )

        if output_mode == "kb_only":
            # Skip image downloads entirely — build an empty bundle dir
            from generator.file_packager import build_job_bundle_name
            bundle_name = build_job_bundle_name(job_label, extracted_at)
            bundle_output_dir = Path(settings.output_dir) / job_id / bundle_name
            bundle_output_dir.mkdir(parents=True, exist_ok=True)
        else:
            bundle_output_dir = await _download_selected_property_images(
                job_id=job_id,
                properties_to_publish=properties_with_kb,
                job_label=job_label,
                extracted_at=extracted_at,
                progress_callback=_update_image_progress,
            )

        total_downloaded_images = sum(
            (property_data.get("images") or {}).get("downloaded", 0)
            for property_data in properties_with_kb
        )

        await update_job(
            job_id,
            status=JobStatus.GENERATING.value,
            progress_pct=98,
            progress_msg="Packaging knowledge base files...",
        )

        async def _update_packaging_progress(
            completed: int,
            total: int,
            property_name: str,
            _result: dict[str, Any],
        ) -> None:
            await update_job(
                job_id,
                progress_pct=_stage_progress(98, 99, completed, total),
                progress_msg=f"Packaged {completed}/{total} property folders ({property_name})",
            )

        package_result = await package_all_properties(
            properties_results=properties_with_kb,
            job_id=job_id,
            source_url=url,
            job_label=job_label,
            extracted_at=extracted_at,
            progress_callback=_update_packaging_progress,
        )

        total_files = package_result.get("total_files", 0)
        if total_files <= 0:
            raise RuntimeError("No knowledge base files were generated from the selected properties.")

        job_output_dir = package_result.get("output_dir") or str(bundle_output_dir.resolve())
        valid_properties = sum(
            1
            for property_result in package_result.get("properties", [])
            if (property_result.get("validation") or {}).get("is_valid")
        )

        await update_job(
            job_id,
            status=JobStatus.COMPLETED.value,
            progress_pct=100,
            progress_msg=(
                f"Done! Generated {total_files} files for {total_properties} properties "
                f"({total_downloaded_images} images, {valid_properties} validation-ready KBs)"
            ),
            output_dir=job_output_dir,
            completed_at=datetime.now(timezone.utc),
            review_data=json.dumps(review_payload, ensure_ascii=False),
        )

        logger.info(
            "Publish complete | job_id=%s | properties=%d | files=%d",
            job_id,
            total_properties,
            total_files,
        )
    except Exception as exc:
        error_msg = f"Publish error: {exc}"
        logger.error("Publish failed for job %s: %s", job_id, exc, exc_info=True)
        await update_job(
            job_id,
            status=JobStatus.FAILED.value,
            progress_pct=0,
            progress_msg=error_msg,
            error_message=error_msg,
        )


@router.post("/scrape", response_model=ScrapeResponse)
async def start_scrape(request: ScrapeRequest) -> ScrapeResponse:
    """Start crawl discovery and prepare the review manifest."""
    url = request.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")

    _ensure_background_worker_ready()

    output_mode = request.output_mode if request.output_mode in ("both", "kb_only", "media_only") else "both"
    project_settings = {
        "name": request.project_name.strip() or "Grand Hotel Bot",
        "bot_enabled": request.bot_enabled,
        "language": request.language.strip() or "English",
        "specific_urls": _normalise_specific_urls(request.specific_urls),
        "auto_sync": request.auto_sync,
        "output_mode": output_mode,
    }

    job_id = str(uuid.uuid4())

    async with async_session() as session:
        job = ScrapeJob(
            id=job_id,
            url=url,
            status=JobStatus.PENDING.value,
            progress_pct=0,
            progress_msg="Queued...",
        )
        session.add(job)
        await session.commit()

    await enqueue_job_task(
        job_id,
        "phase1",
        {"url": url, "project_settings": project_settings},
        max_attempts=settings.phase1_job_max_attempts,
    )

    return ScrapeResponse(
        job_id=job_id,
        status="pending",
        message="Crawl started. Review will be available after discovery completes.",
    )


@router.get("/review/{job_id}")
async def get_review_data(job_id: str) -> dict[str, Any]:
    """Return the editable review manifest for the content manager UI."""
    async with async_session() as session:
        job = await session.get(ScrapeJob, job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    review_payload = _parse_review_data(job_id, job.review_data)
    if job.status not in _REVIEW_READY_STATUSES and not review_payload:
        raise HTTPException(
            status_code=400,
            detail=f"Review data is still being prepared. Current status: {job.status}",
        )

    if not review_payload:
        raise HTTPException(status_code=404, detail="Review data was not found for this job.")

    return {
        "job_id": job_id,
        "status": job.status,
        "review_data": review_payload,
    }


@router.post("/publish/{job_id}", response_model=ScrapeResponse)
async def publish_review(job_id: str, request: PublishRequest) -> ScrapeResponse:
    """Publish reviewed content into KB files."""
    _ensure_background_worker_ready()

    async with async_session() as session:
        job = await session.get(ScrapeJob, job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status in _PUBLISH_ACTIVE_STATUSES:
        return ScrapeResponse(
            job_id=job_id,
            status="processing",
            message=f"Job is already publishing. Current status: {job.status}",
        )

    if job.status == JobStatus.COMPLETED.value:
        return ScrapeResponse(
            job_id=job_id,
            status="completed",
            message="Job already completed.",
        )

    if job.status != JobStatus.PROPERTIES_DETECTED.value:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot publish content in the current status: {job.status}",
        )

    review_payload = request.review_data or _parse_review_data(job_id, job.review_data)
    if not review_payload:
        raise HTTPException(status_code=400, detail="No review data was provided for publishing.")

    await update_job(
        job_id,
        status=JobStatus.EXTRACTING.value,
        progress_pct=76,
        progress_msg="Queued reviewed content for publishing...",
        review_data=json.dumps(review_payload, ensure_ascii=False),
    )
    await enqueue_job_task(
        job_id,
        "publish",
        {"review_data": review_payload},
        max_attempts=settings.publish_job_max_attempts,
    )

    return ScrapeResponse(
        job_id=job_id,
        status="processing",
        message="Publishing reviewed content to the knowledge base.",
    )


@router.post("/jobs/{job_id}/stop", response_model=ScrapeResponse)
async def stop_session(job_id: str) -> ScrapeResponse:
    """Stop a queued or running crawl/publish session and keep it resumable."""
    async with async_session() as session:
        job = await session.get(ScrapeJob, job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if not (job.task_type or "").strip():
        raise HTTPException(status_code=400, detail="This session has no active background task to stop.")

    if job.status in {JobStatus.COMPLETED.value, JobStatus.FAILED.value}:
        raise HTTPException(status_code=400, detail=f"Cannot stop a finished session in status: {job.status}")

    if job.queue_state == QUEUE_STOPPED:
        return ScrapeResponse(
            job_id=job_id,
            status=JobStatus.STOPPED.value,
            message="Session is already stopped.",
        )

    if job.queue_state not in {QUEUE_QUEUED, QUEUE_RUNNING, QUEUE_RETRY_WAIT}:
        raise HTTPException(status_code=400, detail=f"Session cannot be stopped in queue state: {job.queue_state or 'idle'}")

    updated_job = await stop_job_task(job_id)
    if updated_job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    message = (
        "Stop requested. The current session will halt shortly."
        if job.queue_state == QUEUE_RUNNING
        else "Session stopped. Resume when ready."
    )
    return ScrapeResponse(
        job_id=job_id,
        status=JobStatus.STOPPED.value,
        message=message,
    )


@router.post("/jobs/{job_id}/resume", response_model=ScrapeResponse)
async def resume_session(job_id: str) -> ScrapeResponse:
    """Resume a previously stopped crawl/publish session."""
    async with async_session() as session:
        job = await session.get(ScrapeJob, job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.queue_state != QUEUE_STOPPED:
        raise HTTPException(status_code=400, detail=f"Session is not stopped. Current queue state: {job.queue_state or 'idle'}")

    if not (job.task_type or "").strip():
        raise HTTPException(status_code=400, detail="This session does not have resumable background work.")

    _ensure_background_worker_ready()
    updated_job = await resume_job_task(job_id)
    if updated_job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    return ScrapeResponse(
        job_id=job_id,
        status=updated_job.status,
        message="Session queued to resume.",
    )


@router.delete("/jobs/{job_id}", response_model=ScrapeResponse)
async def delete_session(job_id: str) -> ScrapeResponse:
    """Delete an old session and remove associated files."""
    async with async_session() as session:
        job = await session.get(ScrapeJob, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        if job.queue_state in {QUEUE_QUEUED, QUEUE_RUNNING, QUEUE_RETRY_WAIT}:
            raise HTTPException(
                status_code=400,
                detail="Cannot delete an active session. Stop it first.",
            )

        output_dir = job.output_dir or ""

        await session.delete(job)
        await session.commit()

    try:
        _remove_job_artifacts(job_id, output_dir, {})
    except Exception as exc:
        logger.warning("Deleted job %s but failed cleanup: %s", job_id, exc, exc_info=True)

    return ScrapeResponse(
        job_id=job_id,
        status="deleted",
        message="Session deleted.",
    )


@router.get("/properties/{job_id}", response_model=PropertiesResponse)
async def get_properties(job_id: str) -> PropertiesResponse:
    """Legacy property summary endpoint for compatibility."""
    async with async_session() as session:
        job = await session.get(ScrapeJob, job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    properties_data = _parse_properties_data(job_id, job.properties_data)
    if not properties_data:
        review_payload = _parse_review_data(job_id, job.review_data)
        for entity in review_payload.get("entities", []):
            properties_data[entity.get("name", "Property")] = {
                "page_count": entity.get("stats", {}).get("pages", 0),
                "sample_urls": [
                    item.get("url", "")
                    for item in entity.get("items", [])
                    if item.get("type") == "page"
                ][:5],
                "sample_titles": [
                    item.get("name", "")
                    for item in entity.get("items", [])
                    if item.get("type") == "page"
                ][:5],
            }

    properties = [
        PropertyInfo(
            name=name,
            page_count=info.get("page_count", 0),
            sample_urls=info.get("sample_urls", []),
            sample_titles=info.get("sample_titles", []),
        )
        for name, info in properties_data.items()
    ]
    properties.sort(key=lambda item: (item.name == "General", item.name))

    return PropertiesResponse(
        job_id=job_id,
        status=job.status,
        total_properties=len(properties),
        properties=properties,
    )


@router.post("/process/{job_id}", response_model=ScrapeResponse)
async def process_selected(job_id: str, request: ProcessRequest) -> ScrapeResponse:
    """Legacy compatibility endpoint that maps selected properties to review publishing."""
    async with async_session() as session:
        job = await session.get(ScrapeJob, job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    selected_properties = _normalise_selected_properties(request.selected_properties)
    if not selected_properties:
        raise HTTPException(status_code=400, detail="No properties selected. Select at least one.")

    review_payload = _parse_review_data(job_id, job.review_data)
    if not review_payload:
        raise HTTPException(status_code=400, detail="Review data is not ready yet.")

    selected_set = set(selected_properties)
    filtered_entities = []
    for entity in review_payload.get("entities", []):
        if entity.get("name") not in selected_set:
            continue
        cloned_entity = copy.deepcopy(entity)
        for item in cloned_entity.get("items", []):
            item["enabled"] = item.get("type") != "page" or item.get("enabled", True)
        filtered_entities.append(cloned_entity)

    review_payload["entities"] = filtered_entities
    return await publish_review(job_id, PublishRequest(review_data=review_payload))


@router.get("/status/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str) -> JobStatusResponse:
    """Return the current job status and progress."""
    async with async_session() as session:
        job = await session.get(ScrapeJob, job_id)
        if job:
            await reconcile_completed_job(session, job)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return JobStatusResponse(
        job_id=job.id,
        status=job.status,
        queue_state=job.queue_state or "",
        task_type=job.task_type or "",
        progress_pct=job.progress_pct or 0,
        progress_msg=job.progress_msg or "",
        pages_found=job.pages_found or 0,
        pages_crawled=job.pages_crawled or 0,
        pages_failed=job.pages_failed or 0,
        properties_found=job.properties_found or 0,
        kb_preview=job.kb_preview or "",
        error_message=job.error_message or "",
        output_dir=job.output_dir or "",
        session_name=_session_name_for_job(job),
        can_stop=_can_stop(job),
        can_resume=_can_resume(job),
        can_open_review=_can_open_review(job),
        can_download=_can_download(job),
        created_at=job.created_at.isoformat() if job.created_at else "",
        completed_at=job.completed_at.isoformat() if job.completed_at else None,
    )


@router.get("/jobs", response_model=list[JobSummary])
async def list_jobs() -> list[JobSummary]:
    """List recent scrape jobs."""
    async with async_session() as session:
        result = await session.execute(
            select(ScrapeJob).order_by(ScrapeJob.created_at.desc()).limit(50)
        )
        jobs = result.scalars().all()
        for job in jobs:
            await reconcile_completed_job(session, job)

    return [
        JobSummary(
            job_id=job.id,
            session_name=_session_name_for_job(job),
            url=job.url,
            status=job.status,
            queue_state=job.queue_state or "",
            task_type=job.task_type or "",
            progress_pct=job.progress_pct or 0,
            progress_msg=job.progress_msg or "",
            can_stop=_can_stop(job),
            can_resume=_can_resume(job),
            can_open_review=_can_open_review(job),
            can_download=_can_download(job),
            output_dir=job.output_dir or "",
            created_at=job.created_at.isoformat() if job.created_at else "",
            completed_at=job.completed_at.isoformat() if job.completed_at else None,
        )
        for job in jobs
    ]
