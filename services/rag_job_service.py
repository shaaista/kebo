"""
RAG Job Service

In-memory background indexing job manager for admin-triggered RAG rebuilds.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Optional
from uuid import uuid4

from services.rag_service import rag_service, RAGService


@dataclass
class RAGIndexJob:
    job_id: str
    tenant_id: str
    business_type: str
    clear_existing: bool
    file_paths: list[str] = field(default_factory=list)
    status: str = "queued"  # queued | running | completed | failed
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error: str = ""
    report: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "tenant_id": self.tenant_id,
            "business_type": self.business_type,
            "clear_existing": self.clear_existing,
            "file_paths": self.file_paths,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "error": self.error,
            "report": self.report,
        }


class RAGJobService:
    """Tracks and executes background RAG indexing jobs."""

    def __init__(self, rag: RAGService | None = None):
        self._rag_service = rag or rag_service
        self._jobs: dict[str, RAGIndexJob] = {}
        self._job_order: list[str] = []
        self._lock = asyncio.Lock()
        self._max_jobs = 200

    async def start_index_job(
        self,
        tenant_id: str,
        business_type: str,
        clear_existing: bool = True,
        file_paths: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        job_id = uuid4().hex
        job = RAGIndexJob(
            job_id=job_id,
            tenant_id=str(tenant_id or "default"),
            business_type=str(business_type or "generic"),
            clear_existing=bool(clear_existing),
            file_paths=list(file_paths or []),
        )

        async with self._lock:
            self._jobs[job_id] = job
            self._job_order.append(job_id)
            while len(self._job_order) > self._max_jobs:
                old_id = self._job_order.pop(0)
                self._jobs.pop(old_id, None)

        asyncio.create_task(self._run_job(job_id))
        return job.to_dict()

    async def _run_job(self, job_id: str) -> None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.status = "running"
            job.started_at = datetime.now(UTC)

        try:
            report = await self._rag_service.ingest_from_knowledge_base(
                tenant_id=job.tenant_id,
                business_type=job.business_type,
                clear_existing=job.clear_existing,
                file_paths=job.file_paths or None,
            )
            async with self._lock:
                stored = self._jobs.get(job_id)
                if not stored:
                    return
                stored.report = report
                stored.status = "completed"
                stored.finished_at = datetime.now(UTC)
        except Exception as exc:
            async with self._lock:
                stored = self._jobs.get(job_id)
                if not stored:
                    return
                stored.error = str(exc)
                stored.status = "failed"
                stored.finished_at = datetime.now(UTC)

    async def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        async with self._lock:
            job = self._jobs.get(job_id)
            return job.to_dict() if job else None

    async def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        async with self._lock:
            selected = list(reversed(self._job_order))[:limit]
            return [self._jobs[job_id].to_dict() for job_id in selected if job_id in self._jobs]

    async def wait_for_job(self, job_id: str, timeout_seconds: float = 10.0) -> Optional[dict[str, Any]]:
        """Test helper: wait until a job reaches terminal state."""
        deadline = asyncio.get_running_loop().time() + max(0.1, timeout_seconds)
        while asyncio.get_running_loop().time() < deadline:
            job = await self.get_job(job_id)
            if not job:
                return None
            if job["status"] in {"completed", "failed"}:
                return job
            await asyncio.sleep(0.05)
        return await self.get_job(job_id)


rag_job_service = RAGJobService()

