"""Background worker process for queued scrape jobs."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import uuid

from api.routes.scrape import _run_phase1, _run_publish
from config.settings import settings
from models.job import JobStatus, ScrapeJob, async_session, init_db
from services.job_queue import (
    QueuedTask,
    claim_next_job,
    get_task_retry_delay_seconds,
    heartbeat_job,
    mark_task_failed,
    mark_task_for_retry,
    mark_task_stopped,
    mark_task_succeeded,
    wait_for_job_stop,
)
from services.metrics import metrics

logger = logging.getLogger(__name__)

LOG_DIR = os.path.join(settings.output_dir, "logs")
LOG_FILE = os.path.join(LOG_DIR, "hotel-kb-scraper.log")


def _configure_worker_logging() -> None:
    """Ensure the standalone worker writes to the shared application log."""
    if logging.getLogger().handlers:
        return

    os.makedirs(LOG_DIR, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(name)-30s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ],
    )


async def execute_job_task(task: QueuedTask) -> None:
    """Dispatch a claimed job to the correct background runner."""
    if task.task_type == "phase1":
        await _run_phase1(task.job_id, task.task_payload.get("url", task.url), task.task_payload.get("project_settings", {}))
        return

    if task.task_type == "publish":
        await _run_publish(task.job_id, task.url, task.task_payload.get("review_data", {}))
        return

    raise RuntimeError(f"Unsupported task type: {task.task_type}")


async def _heartbeat_loop(job_id: str, worker_id: str) -> None:
    """Send regular heartbeats while a job is running."""
    try:
        while True:
            await asyncio.sleep(settings.worker_heartbeat_interval_seconds)
            await heartbeat_job(job_id, worker_id)
    except asyncio.CancelledError:
        raise


async def _load_job(job_id: str) -> ScrapeJob | None:
    async with async_session() as session:
        return await session.get(ScrapeJob, job_id)


async def process_next_queued_job(worker_id: str) -> bool:
    """Claim and execute a single queued job."""
    task = await claim_next_job(worker_id)
    if task is None:
        return False

    heartbeat_task = asyncio.create_task(_heartbeat_loop(task.job_id, worker_id))
    execution_task = asyncio.create_task(execute_job_task(task))
    stop_monitor_task = asyncio.create_task(wait_for_job_stop(task.job_id))
    try:
        done, _ = await asyncio.wait(
            {execution_task, stop_monitor_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if stop_monitor_task in done and not execution_task.done():
            execution_task.cancel()
            try:
                await execution_task
            except asyncio.CancelledError:
                pass

            await mark_task_stopped(task.job_id, worker_id)
            metrics.record_worker_task_result(
                task_type=task.task_type,
                result="stopped",
                job_id=task.job_id,
            )
            logger.info("Worker stop acknowledged for job %s", task.job_id)
            return True

        stop_monitor_task.cancel()
        try:
            await stop_monitor_task
        except asyncio.CancelledError:
            pass

        await execution_task
        job = await _load_job(task.job_id)
        if job is not None and job.status == JobStatus.FAILED.value:
            raise RuntimeError(job.error_message or f"{task.task_type} failed")

        await mark_task_succeeded(task.job_id, worker_id)
        metrics.record_worker_task_result(task_type=task.task_type, result="succeeded", job_id=task.job_id)
    except Exception as exc:
        if task.queue_attempts < task.max_attempts:
            delay_seconds = get_task_retry_delay_seconds(task.task_type, task.queue_attempts)
            await mark_task_for_retry(
                task.job_id,
                worker_id,
                error_message=str(exc),
                delay_seconds=delay_seconds,
            )
            metrics.record_worker_task_result(task_type=task.task_type, result="retry", job_id=task.job_id)
        else:
            await mark_task_failed(task.job_id, worker_id, error_message=str(exc))
            metrics.record_worker_task_result(task_type=task.task_type, result="failed", job_id=task.job_id)
        logger.exception("Worker execution failed for job %s", task.job_id)
    finally:
        heartbeat_task.cancel()
        stop_monitor_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        try:
            await stop_monitor_task
        except asyncio.CancelledError:
            pass

    return True


async def worker_loop(*, worker_id: str | None = None, run_once: bool = False) -> None:
    """Continuously process queued jobs."""
    await init_db()
    effective_worker_id = worker_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
    logger.info("Worker started: %s", effective_worker_id)

    while True:
        handled = await process_next_queued_job(effective_worker_id)
        if run_once:
            return
        if not handled:
            await asyncio.sleep(settings.worker_poll_interval_seconds)


def main() -> None:
    """CLI entrypoint for the worker process."""
    _configure_worker_logging()
    parser = argparse.ArgumentParser(description="Hotel KB Scraper background worker")
    parser.add_argument("--once", action="store_true", help="Process at most one queued job and exit.")
    parser.add_argument("--worker-id", default="", help="Override the generated worker ID.")
    args = parser.parse_args()
    try:
        asyncio.run(worker_loop(worker_id=args.worker_id or None, run_once=args.once))
    except KeyboardInterrupt:
        logger.info("Worker stopped by user.")


if __name__ == "__main__":
    main()
