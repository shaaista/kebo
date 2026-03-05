# RAG Agent Backlog

Owner module: `agents/rag_agent.py`

## Mission
Build a production-grade, industry-agnostic RAG capability for all channels
(web widget + WhatsApp) with strict tenant/business isolation.

## Current status
- [x] Agent boundary created (`RAGAgent.answer`)
- [x] FAQ handler uses RAG agent first, then fallback
- [x] Local knowledge-base retrieval bootstrap
- [x] Complex-path agent orchestration integrated in chat routing (`complex_query_orchestrator`)

## Next tasks
- [x] Add ingestion pipeline (chunking + metadata extraction)
- [x] Add vector DB backend (Qdrant) with tenant metadata filters
- [x] Add reranker stage for better precision
- [~] Add admin upload endpoint + indexing jobs (reindex/status/query endpoints done; file upload + background jobs pending)
- [ ] Add per-industry knowledge collections
- [ ] Add evaluation set and retrieval quality metrics

## Contract (stable interface)
Input:
- query
- hotel_name
- city
- min_confidence

Output:
- handled (bool)
- answer (str)
- confidence (float)
- sources (list[str])
- reason (str)
