"""Database-backed queue helpers for background jobs."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select

from config.settings import settings
from models.job import JobStatus, ScrapeJob, async_session
from services.metrics import metrics

logger = logging.getLogger(__name__)

QUEUE_IDLE = "idle"
QUEUE_QUEUED = "queued"
QUEUE_RUNNING = "running"
QUEUE_RETRY_WAIT = "retry_wait"
QUEUE_STOPPED = "stopped"


@dataclass(slots=True)
class QueuedTask:
    """Claimed task payload returned to the worker."""

    job_id: str
    url: str
    task_type: str
    task_payload: dict[str, Any]
    queue_attempts: int
    max_attempts: int


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_json(payload: str | None) -> dict[str, Any]:
    if not payload:
        return {}
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _stopped_message(task_type: str, *, running: bool) -> str:
    """Return a user-facing message for a stopped queue task."""
    if task_type == "publish":
        return (
            "Stopping current publish session..."
            if running
            else "Publish session stopped. Resume when ready."
        )
    return (
        "Stopping current crawl session..."
        if running
        else "Crawl session stopped. Resume when ready."
    )


def _resume_message(task_type: str) -> tuple[str, str]:
    """Return the status/progress message used when a stopped task is resumed."""
    if task_type == "publish":
        return JobStatus.PENDING.value, "Publish session queued to resume..."
    return JobStatus.PENDING.value, "Crawl session queued to resume..."


async def enqueue_job_task(
    job_id: str,
    task_type: str,
    payload: dict[str, Any],
    *,
    max_attempts: int,
) -> None:
    """Queue a background task for a job."""
    async with async_session() as session:
        job = await session.get(ScrapeJob, job_id)
        if job is None:
            raise ValueError(f"Job {job_id} does not exist")

        job.task_type = task_type
        job.task_payload = json.dumps(payload, ensure_ascii=False)
        job.queue_state = QUEUE_QUEUED
        job.queue_attempts = 0
        job.max_attempts = max(1, max_attempts)
        job.next_retry_at = _utcnow()
        job.worker_id = ""
        job.worker_started_at = None
        job.worker_heartbeat_at = None
        await session.commit()

    metrics.record_job_enqueued(task_type)


async def claim_next_job(worker_id: str) -> QueuedTask | None:
    """Claim the next queued or retry-ready job for a worker."""
    now = _utcnow()

    async with async_session() as session:
        result = await session.execute(
            select(ScrapeJob)
            .where(
                or_(
                    and_(
                        ScrapeJob.queue_state == QUEUE_QUEUED,
                        or_(ScrapeJob.next_retry_at.is_(None), ScrapeJob.next_retry_at <= now),
                    ),
                    and_(
                        ScrapeJob.queue_state == QUEUE_RETRY_WAIT,
                        ScrapeJob.next_retry_at <= now,
                    ),
                )
            )
            .order_by(ScrapeJob.created_at.asc())
            .limit(1)
        )
        job = result.scalars().first()
        if job is None:
            return None

        job.queue_state = QUEUE_RUNNING
        job.worker_id = worker_id
        job.worker_started_at = now
        job.worker_heartbeat_at = now
        job.queue_attempts = (job.queue_attempts or 0) + 1
        await session.commit()
        await session.refresh(job)

        return QueuedTask(
            job_id=job.id,
            url=job.url,
            task_type=job.task_type or "",
            task_payload=_parse_json(job.task_payload),
            queue_attempts=job.queue_attempts or 0,
            max_attempts=job.max_attempts or 1,
        )


async def heartbeat_job(job_id: str, worker_id: str) -> None:
    """Update the worker heartbeat for a claimed job."""
    async with async_session() as session:
        job = await session.get(ScrapeJob, job_id)
        if job is None or job.worker_id != worker_id or job.queue_state != QUEUE_RUNNING:
            return

        job.worker_heartbeat_at = _utcnow()
        await session.commit()


async def mark_task_succeeded(job_id: str, worker_id: str) -> None:
    """Clear queue metadata after a successful worker run."""
    async with async_session() as session:
        job = await session.get(ScrapeJob, job_id)
        if job is None or job.worker_id != worker_id:
            return

        job.queue_state = QUEUE_IDLE
        job.task_type = ""
        job.task_payload = ""
        job.worker_id = ""
        job.worker_started_at = None
        job.worker_heartbeat_at = None
        job.next_retry_at = None
        await session.commit()


async def mark_task_stopped(job_id: str, worker_id: str) -> None:
    """Clear worker ownership after a stop request while preserving resume context."""
    async with async_session() as session:
        job = await session.get(ScrapeJob, job_id)
        if job is None or job.worker_id != worker_id:
            return

        job.queue_state = QUEUE_STOPPED
        job.status = JobStatus.STOPPED.value
        job.progress_msg = _stopped_message(job.task_type or "", running=False)
        job.worker_id = ""
        job.worker_started_at = None
        job.worker_heartbeat_at = None
        job.next_retry_at = None
        await session.commit()


async def mark_task_for_retry(job_id: str, worker_id: str, *, error_message: str, delay_seconds: float) -> None:
    """Schedule a claimed job to be retried later."""
    async with async_session() as session:
        job = await session.get(ScrapeJob, job_id)
        if job is None or job.worker_id != worker_id:
            return

        job.queue_state = QUEUE_RETRY_WAIT
        job.error_message = error_message
        job.worker_id = ""
        job.worker_started_at = None
        job.worker_heartbeat_at = None
        job.next_retry_at = _utcnow() + timedelta(seconds=max(0.0, delay_seconds))
        await session.commit()


async def mark_task_failed(job_id: str, worker_id: str, *, error_message: str) -> None:
    """Clear queue metadata after a terminal worker failure."""
    async with async_session() as session:
        job = await session.get(ScrapeJob, job_id)
        if job is None or job.worker_id != worker_id:
            return

        job.queue_state = QUEUE_IDLE
        job.error_message = error_message
        job.worker_id = ""
        job.worker_started_at = None
        job.worker_heartbeat_at = None
        job.next_retry_at = None
        await session.commit()


async def stop_job_task(job_id: str) -> ScrapeJob | None:
    """Stop a queued or running task while keeping enough context for resume."""
    async with async_session() as session:
        job = await session.get(ScrapeJob, job_id)
        if job is None:
            return None

        if not (job.task_type or "").strip():
            return job

        if job.queue_state == QUEUE_STOPPED and job.status == JobStatus.STOPPED.value:
            return job

        running = job.queue_state == QUEUE_RUNNING
        if job.queue_state not in {QUEUE_QUEUED, QUEUE_RETRY_WAIT, QUEUE_RUNNING, QUEUE_STOPPED}:
            return job

        job.queue_state = QUEUE_STOPPED
        job.status = JobStatus.STOPPED.value
        job.progress_msg = _stopped_message(job.task_type or "", running=running)
        job.next_retry_at = None

        if not running:
            job.worker_id = ""
            job.worker_started_at = None
            job.worker_heartbeat_at = None

        await session.commit()
        await session.refresh(job)
        return job


async def resume_job_task(job_id: str) -> ScrapeJob | None:
    """Resume a previously stopped queued task."""
    async with async_session() as session:
        job = await session.get(ScrapeJob, job_id)
        if job is None:
            return None

        if job.queue_state != QUEUE_STOPPED or not (job.task_type or "").strip():
            return job

        resume_status, resume_message = _resume_message(job.task_type or "")
        job.queue_state = QUEUE_QUEUED
        job.status = resume_status
        job.progress_msg = resume_message
        job.worker_id = ""
        job.worker_started_at = None
        job.worker_heartbeat_at = None
        job.next_retry_at = _utcnow()
        await session.commit()
        await session.refresh(job)
        return job


async def interrupt_stale_running_jobs() -> int:
    """Convert stale running tasks into explicit stopped sessions."""
    stale_cutoff = _utcnow() - timedelta(seconds=settings.worker_stale_after_seconds)

    async with async_session() as session:
        result = await session.execute(
            select(ScrapeJob).where(
                and_(
                    ScrapeJob.queue_state == QUEUE_RUNNING,
                    or_(
                        ScrapeJob.worker_heartbeat_at.is_(None),
                        ScrapeJob.worker_heartbeat_at < stale_cutoff,
                    ),
                )
            )
        )
        stale_jobs = result.scalars().all()
        if not stale_jobs:
            return 0

        for job in stale_jobs:
            job.queue_state = QUEUE_STOPPED
            job.status = JobStatus.STOPPED.value
            job.progress_msg = "Session stopped after restart. Resume when ready."
            job.worker_id = ""
            job.worker_started_at = None
            job.worker_heartbeat_at = None
            job.next_retry_at = None

        await session.commit()
        return len(stale_jobs)


async def wait_for_job_stop(job_id: str, *, poll_interval_seconds: float = 0.5) -> None:
    """Block until the given job is marked as stopped."""
    while True:
        async with async_session() as session:
            job = await session.get(ScrapeJob, job_id)
            if job is None:
                return
            if job.queue_state == QUEUE_STOPPED or job.status == JobStatus.STOPPED.value:
                return

        await asyncio.sleep(poll_interval_seconds)


def get_task_retry_delay_seconds(task_type: str, attempt_number: int) -> float:
    """Return the configured worker retry delay for a task attempt."""
    delays = (
        settings.publish_job_retry_backoff_seconds
        if task_type == "publish"
        else settings.phase1_job_retry_backoff_seconds
    )
    index = min(max(attempt_number - 1, 0), len(delays) - 1)
    return float(delays[index])


async def get_queue_metrics_snapshot() -> dict[str, int]:
    """Return queue counts and stale-running worker counts."""
    snapshot = {
        QUEUE_IDLE: 0,
        QUEUE_QUEUED: 0,
        QUEUE_RUNNING: 0,
        QUEUE_RETRY_WAIT: 0,
        QUEUE_STOPPED: 0,
        "stale_running": 0,
    }
    stale_cutoff = _utcnow() - timedelta(seconds=settings.worker_stale_after_seconds)

    async with async_session() as session:
        rows = await session.execute(
            select(ScrapeJob.queue_state, func.count()).group_by(ScrapeJob.queue_state)
        )
        for state_name, count in rows.all():
            if state_name:
                snapshot[state_name] = int(count)

        stale_result = await session.execute(
            select(func.count())
            .select_from(ScrapeJob)
            .where(
                and_(
                    ScrapeJob.queue_state == QUEUE_RUNNING,
                    or_(
                        ScrapeJob.worker_heartbeat_at.is_(None),
                        ScrapeJob.worker_heartbeat_at < stale_cutoff,
                    ),
                )
            )
        )
        snapshot["stale_running"] = int(stale_result.scalar_one() or 0)

    return snapshot
