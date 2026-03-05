from pathlib import Path

import pytest

from services.rag_job_service import RAGJobService
from services.rag_service import RAGService


@pytest.mark.asyncio
async def test_rag_job_service_completes_index_job(tmp_path: Path):
    kb_dir = tmp_path / "kb"
    kb_dir.mkdir(parents=True, exist_ok=True)
    (kb_dir / "faq.md").write_text(
        "Breakfast starts at 7 AM and checkout time is 11 AM.",
        encoding="utf-8",
    )

    rag = RAGService(
        kb_dir=kb_dir,
        local_index_file=tmp_path / "rag" / "index.json",
        backend="local",
    )
    jobs = RAGJobService(rag)

    job = await jobs.start_index_job(
        tenant_id="tenant_hotel",
        business_type="hotel",
        clear_existing=True,
        file_paths=None,
    )
    final = await jobs.wait_for_job(job["job_id"], timeout_seconds=5.0)

    assert final is not None
    assert final["status"] == "completed"
    assert final["report"]["chunks_indexed"] >= 1
    assert final["tenant_id"] == "tenant_hotel"


@pytest.mark.asyncio
async def test_rag_job_service_marks_failure():
    class BrokenRag:
        async def ingest_from_knowledge_base(self, **kwargs):
            raise RuntimeError("index failed")

    jobs = RAGJobService(BrokenRag())
    job = await jobs.start_index_job(
        tenant_id="tenant_fail",
        business_type="generic",
        clear_existing=True,
        file_paths=[],
    )
    final = await jobs.wait_for_job(job["job_id"], timeout_seconds=5.0)

    assert final is not None
    assert final["status"] == "failed"
    assert "index failed" in final["error"]
