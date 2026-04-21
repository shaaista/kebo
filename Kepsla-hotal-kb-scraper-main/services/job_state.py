"""Helpers for reconciling stale local job state after interrupted reruns."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import settings
from models.job import JobStatus, ScrapeJob
from services.job_queue import QUEUE_QUEUED, QUEUE_RETRY_WAIT, QUEUE_RUNNING, QUEUE_STOPPED

_ACTIVE_QUEUE_STATES = {QUEUE_QUEUED, QUEUE_RUNNING, QUEUE_RETRY_WAIT, QUEUE_STOPPED}


def has_completed_job_artifacts(job: ScrapeJob) -> bool:
    """Return True when the filesystem still contains a completed bundle for the job."""
    output_dir = (job.output_dir or "").strip()
    if output_dir:
        bundle_dir = Path(output_dir)
        if bundle_dir.exists() and any(bundle_dir.rglob("*_kb.txt")):
            return True

    job_root = Path(settings.output_dir) / job.id
    if not job_root.exists():
        return False

    return any(job_root.glob("*.zip"))


def should_recover_completed_job(job: ScrapeJob) -> bool:
    """Identify orphaned in-progress rows that already have a finished bundle."""
    if job.status == JobStatus.COMPLETED.value:
        return False

    if (job.queue_state or "").strip() in _ACTIVE_QUEUE_STATES:
        return False

    if job.completed_at is None:
        return False

    return has_completed_job_artifacts(job)


async def reconcile_completed_job(session: AsyncSession, job: ScrapeJob) -> bool:
    """Repair stale job rows left behind by an interrupted rerun."""
    if not should_recover_completed_job(job):
        return False

    job.status = JobStatus.COMPLETED.value
    job.progress_pct = 100
    if not (job.progress_msg or "").startswith("Done!"):
        job.progress_msg = (
            "Done! Recovered the last completed bundle after an interrupted rerun."
        )
    job.error_message = ""
    await session.commit()
    return True
