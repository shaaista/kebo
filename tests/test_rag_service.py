from pathlib import Path
import json

import pytest

from services.rag_service import RAGService, RetrievedChunk


@pytest.mark.asyncio
async def test_rag_local_tenant_isolation(tmp_path: Path):
    index_file = tmp_path / "rag" / "index.json"
    service = RAGService(kb_dir=tmp_path / "kb", local_index_file=index_file, backend="local")
    service.chunk_size = 40
    service.chunk_overlap = 8

    docs_tenant_a = [
        ("hotel_policy.md", "Checkout time is 11 AM and breakfast starts at 7 AM."),
    ]
    docs_tenant_b = [
        ("clinic_policy.md", "Consultation starts at 9 AM and OPD closes at 6 PM."),
    ]

    await service.ingest_documents(docs_tenant_a, tenant_id="tenant_a", business_type="hotel", clear_existing=True)
    await service.ingest_documents(docs_tenant_b, tenant_id="tenant_b", business_type="healthcare", clear_existing=True)

    chunks_a = await service.retrieve("what is checkout time", tenant_id="tenant_a", top_k=3)
    chunks_b = await service.retrieve("what is checkout time", tenant_id="tenant_b", top_k=3)

    assert chunks_a
    assert "checkout time" in chunks_a[0].content.lower()
    assert chunks_a[0].metadata.get("tenant_id") == "tenant_a"
    # tenant_b should not receive tenant_a chunk for a tenant-scoped query
    assert not chunks_b or "checkout time" not in chunks_b[0].content.lower()


@pytest.mark.asyncio
async def test_rag_ingest_from_kb_files(tmp_path: Path):
    kb_dir = tmp_path / "knowledge"
    kb_dir.mkdir(parents=True, exist_ok=True)
    (kb_dir / "faq.md").write_text(
        "Room service is available 24/7. Laundry delivery is within 2 hours.",
        encoding="utf-8",
    )

    index_file = tmp_path / "rag" / "local_index.json"
    service = RAGService(kb_dir=kb_dir, local_index_file=index_file, backend="local")
    service.chunk_size = 20
    service.chunk_overlap = 5

    report = await service.ingest_from_knowledge_base(tenant_id="tenant_docs", clear_existing=True)

    assert report["documents_ingested"] == 1
    assert report["chunks_indexed"] >= 1
    assert index_file.exists()

    chunks = await service.retrieve("Is room service available", tenant_id="tenant_docs", top_k=2)
    assert chunks
    assert "room service" in chunks[0].content.lower()


@pytest.mark.asyncio
async def test_rag_qdrant_backend_graceful_fallback(tmp_path: Path):
    index_file = tmp_path / "rag" / "index.json"
    service = RAGService(kb_dir=tmp_path / "kb", local_index_file=index_file, backend="qdrant")
    service.chunk_size = 30
    service.chunk_overlap = 5

    docs = [("travel.md", "Airport shuttle is available every 30 minutes.")]
    report = await service.ingest_documents(docs, tenant_id="tenant_q", clear_existing=True)

    # If qdrant is not configured/available, local fallback must still work.
    assert report["chunks_indexed"] == 1
    chunks = await service.retrieve("Do you have airport shuttle", tenant_id="tenant_q", top_k=2)
    assert chunks
    assert "airport shuttle" in chunks[0].content.lower()


@pytest.mark.asyncio
async def test_rag_ingest_wrapped_json_txt_prioritizes_ird_menu_sections(tmp_path: Path):
    kb_dir = tmp_path / "knowledge"
    kb_dir.mkdir(parents=True, exist_ok=True)

    wrapped = {
        "data": json.dumps(
            {
                "editable": {
                    "Hotel Sales Script": (
                        "Starting in the lobby with first impressions and design narrative. "
                        "This section is long-form storytelling content and not a food catalog."
                    ),
                    "All day section - IN ROOM DINING (IRD) MENU": (
                        "menu_name: IRD\n"
                        "section_name: All Day\n"
                        "item_name: Margherita Pizza\n"
                        "price_inr: 450\n"
                        "item_name: Chicken Alfredo Pasta\n"
                        "price_inr: 480\n"
                    ),
                    "Beverages section - IN ROOM DINING (IRD) MENU": (
                        "menu_name: IRD\n"
                        "section_name: Beverages\n"
                        "item_name: Fresh Lime Soda\n"
                        "price_inr: 120\n"
                    ),
                }
            }
        ),
        "orgId": "8899",
    }
    (kb_dir / "wrapped.txt").write_text(json.dumps(wrapped), encoding="utf-8")

    index_file = tmp_path / "rag" / "local_index.json"
    service = RAGService(kb_dir=kb_dir, local_index_file=index_file, backend="local")
    service.chunk_size = 60
    service.chunk_overlap = 10

    report = await service.ingest_from_knowledge_base(tenant_id="tenant_menu", clear_existing=True)
    assert report["documents_ingested"] == 1
    assert report["chunks_indexed"] >= 1

    chunks = await service.retrieve(
        "i need food show us your menus",
        tenant_id="tenant_menu",
        top_k=4,
    )
    assert chunks

    joined_top = " ".join(chunk.content.lower() for chunk in chunks[:2])
    assert "in room dining" in joined_top or "ird" in joined_top or "item_name" in joined_top
    assert any(chunk.metadata.get("section") for chunk in chunks)


@pytest.mark.asyncio
async def test_rag_retrieves_wrapped_json_txt_for_in_room_menu_query(tmp_path: Path):
    kb_dir = tmp_path / "knowledge"
    kb_dir.mkdir(parents=True, exist_ok=True)

    wrapped = {
        "data": json.dumps(
            {
                "editable": {
                    "Midnight selections - IN-ROOM_DINING (IRD)": (
                        "menu_name: IRD Midnight\n"
                        "section_name: Midnight\n"
                        "item_name: Butter Chicken\n"
                        "price_inr: 520\n"
                    ),
                    "Kadak Menu": (
                        "menu_name: Kadak\n"
                        "section_name: Soup\n"
                        "item_name: Tomato Saar\n"
                        "price_inr: 445\n"
                    ),
                }
            }
        ),
        "orgId": "8899",
    }
    (kb_dir / "property.txt").write_text(json.dumps(wrapped), encoding="utf-8")

    index_file = tmp_path / "rag" / "local_index.json"
    service = RAGService(kb_dir=kb_dir, local_index_file=index_file, backend="local")
    service.chunk_size = 50
    service.chunk_overlap = 8

    await service.ingest_from_knowledge_base(tenant_id="tenant_ird", clear_existing=True)

    chunks = await service.retrieve(
        "show me your in room dining menu so i can order",
        tenant_id="tenant_ird",
        top_k=4,
    )
    assert chunks
    top_text = chunks[0].content.lower()
    assert "in-room_dining" in top_text or "in room dining" in top_text or "ird" in top_text


@pytest.mark.asyncio
async def test_rag_retrieves_checkin_checkout_policy_from_wrapped_json(tmp_path: Path):
    kb_dir = tmp_path / "knowledge"
    kb_dir.mkdir(parents=True, exist_ok=True)

    wrapped = {
        "data": json.dumps(
            {
                "editable": {
                    "checkin_time": "14:00 hrs",
                    "checkout_time": "11:00 hrs",
                    "spa_timings": "9:00 AM - 11:00 PM",
                }
            }
        ),
        "orgId": "8899",
    }
    (kb_dir / "policy.txt").write_text(json.dumps(wrapped), encoding="utf-8")

    index_file = tmp_path / "rag" / "local_index.json"
    service = RAGService(kb_dir=kb_dir, local_index_file=index_file, backend="local")
    service.chunk_size = 40
    service.chunk_overlap = 8

    await service.ingest_from_knowledge_base(tenant_id="tenant_policy", clear_existing=True)

    chunks = await service.retrieve(
        "what are the check in time chcek out time",
        tenant_id="tenant_policy",
        top_k=4,
    )
    assert chunks
    joined = " ".join(chunk.content.lower() for chunk in chunks[:3])
    assert "checkin time" in joined or "checkout time" in joined


@pytest.mark.asyncio
async def test_rag_retrieve_dedupes_duplicate_chunks(tmp_path: Path):
    index_file = tmp_path / "rag" / "index.json"
    service = RAGService(kb_dir=tmp_path / "kb", local_index_file=index_file, backend="local")
    service.chunk_size = 40
    service.chunk_overlap = 8

    docs = [
        ("a.txt", "checkin time is 14:00 hrs and checkout time is 11:00 hrs"),
        ("b.txt", "checkin time is 14:00 hrs and checkout time is 11:00 hrs"),
        ("c.txt", "spa timings are 9:00 AM to 11:00 PM"),
    ]
    await service.ingest_documents(docs, tenant_id="tenant_dupes", clear_existing=True)

    chunks = await service.retrieve("what is check in and check out time", tenant_id="tenant_dupes", top_k=4)
    assert chunks
    signatures = {
        " ".join(chunk.content.lower().split())[:220]
        for chunk in chunks
    }
    assert len(signatures) == len(chunks)


def test_rag_query_variants_normalize_common_typos(tmp_path: Path):
    service = RAGService(kb_dir=tmp_path / "kb", local_index_file=tmp_path / "rag" / "index.json", backend="local")
    variants = service._build_query_variants("whatis chec in timings and chcek out")

    lowered = [variant.lower() for variant in variants]
    assert any("what is" in variant for variant in lowered)
    assert any("check in" in variant for variant in lowered)
    assert any("check out" in variant for variant in lowered)
    assert any("timing" in variant for variant in lowered)


@pytest.mark.asyncio
async def test_rag_retrieve_uses_expanded_candidate_pool(monkeypatch, tmp_path: Path):
    service = RAGService(kb_dir=tmp_path / "kb", local_index_file=tmp_path / "rag" / "index.json", backend="local")
    captured_limits: list[int] = []

    def fake_retrieve_local(question: str, tenant_id: str, top_k: int):
        captured_limits.append(int(top_k))
        return []

    monkeypatch.setattr(service, "_retrieve_local", fake_retrieve_local)

    chunks = await service.retrieve("check in timing", tenant_id="tenant_pool", top_k=3)
    assert chunks == []
    assert captured_limits
    assert all(20 <= value <= 40 for value in captured_limits)


def test_rag_mmr_selects_diverse_chunks_when_candidates_overlap(tmp_path: Path):
    service = RAGService(kb_dir=tmp_path / "kb", local_index_file=tmp_path / "rag" / "index.json", backend="local")
    candidates = [
        RetrievedChunk(
            source="policy_a",
            content="check in time is 14:00 and check out time is 11:00 policy details",
            score=0.93,
            chunk_id="a1",
            metadata={"section": "checkin policy"},
        ),
        RetrievedChunk(
            source="policy_b",
            content="check in time is 14:00 and check out time is 11:00 with extra policy notes",
            score=0.92,
            chunk_id="b1",
            metadata={"section": "arrival policy"},
        ),
        RetrievedChunk(
            source="menu",
            content="in room dining menu section includes margherita pizza and pasta prices",
            score=0.9,
            chunk_id="m1",
            metadata={"section": "ird menu"},
        ),
    ]

    selected = service._select_chunks_mmr(
        question="check in time and menu options",
        chunks=candidates,
        top_k=2,
        lambda_value=0.7,
    )

    assert len(selected) == 2
    combined = " ".join(chunk.content.lower() for chunk in selected)
    assert "check in" in combined
    assert "menu" in combined


@pytest.mark.asyncio
async def test_rag_answer_question_uses_llm_rewritten_query_for_retrieval(monkeypatch, tmp_path: Path):
    service = RAGService(kb_dir=tmp_path / "kb", local_index_file=tmp_path / "rag" / "index.json", backend="local")
    captured: dict[str, str] = {}

    async def fake_rewrite(question: str, business_type: str = "generic") -> str:
        return "what is check in timing"

    async def fake_retrieve(question: str, tenant_id: str = "default", top_k=None, trace=None):
        captured["question"] = question
        return [
            RetrievedChunk(
                source="policy.md",
                content="Check-in time is 14:00 hrs.",
                score=0.9,
                chunk_id="policy:0",
                metadata={"section": "policy"},
            )
        ]

    monkeypatch.setattr(service, "_rewrite_query_with_llm", fake_rewrite)
    monkeypatch.setattr(service, "retrieve", fake_retrieve)

    answer = await service.answer_question(
        question="whatis chec in time",
        hotel_name="Demo",
        city="Mumbai",
        tenant_id="tenant_rewrite",
        business_type="hotel",
    )

    assert answer is not None
    assert captured.get("question") == "what is check in timing"
    assert "check-in time" in answer.answer.lower() or "based on our available information" in answer.answer.lower()
    assert answer.trace_id


@pytest.mark.asyncio
async def test_rag_answer_question_writes_detailed_step_log(tmp_path: Path):
    index_file = tmp_path / "rag" / "index.json"
    service = RAGService(kb_dir=tmp_path / "kb", local_index_file=index_file, backend="local")
    service.step_logs_enabled = True
    service.step_log_file = tmp_path / "logs" / "detailedsteps.log"

    docs = [("policy.md", "Check-in time is 14:00 hrs and checkout time is 11:00 hrs.")]
    await service.ingest_documents(docs, tenant_id="tenant_trace", clear_existing=True)

    answer = await service.answer_question(
        question="whatis chec in timng",
        hotel_name="Demo",
        city="Mumbai",
        tenant_id="tenant_trace",
        business_type="hotel",
    )

    assert answer is not None
    assert answer.trace_id
    assert service.step_log_file.exists()

    log_text = service.step_log_file.read_text(encoding="utf-8")
    assert answer.trace_id in log_text
    assert "rewrite_query_llm" in log_text
    assert "retrieve_start" in log_text
    assert "retrieve_complete" in log_text
    assert "1) USER QUERY" in log_text
    assert "2) REWRITTEN QUERY" in log_text
    assert "3) RAG OUTPUT" in log_text
    assert "4) INPUT SENT TO LLM" in log_text
    assert "5) LLM OUTPUT" in log_text
