"""
RAG Service

Production-oriented retrieval pipeline with:
1) ingestion + chunking + metadata tagging
2) tenant-scoped retrieval (local index or Qdrant backend)
3) optional reranking and grounded answer synthesis
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
import hashlib
import json
import re
import uuid
from typing import Any, Optional

from config.settings import settings
from llm.client import llm_client

try:
    from qdrant_client import QdrantClient
    from qdrant_client.http import models as qdrant_models
except Exception:  # pragma: no cover - optional dependency
    QdrantClient = None
    qdrant_models = None


DEFAULT_KB_DIR = Path(__file__).parent.parent / "config" / "knowledge_base"
_QUERY_STOPWORDS = {
    "a",
    "an",
    "the",
    "i",
    "me",
    "my",
    "we",
    "us",
    "you",
    "your",
    "is",
    "are",
    "am",
    "to",
    "of",
    "in",
    "on",
    "for",
    "with",
    "and",
    "or",
    "do",
    "does",
    "did",
    "can",
    "could",
    "would",
    "should",
    "please",
    "show",
    "list",
    "all",
    "available",
    "have",
    "has",
    "need",
    "want",
    "tell",
    "about",
    "what",
    "which",
    "where",
    "when",
    "how",
}
_CATALOG_QUERY_TERMS = {
    "menu",
    "menus",
    "catalog",
    "options",
    "option",
    "food",
    "hungry",
    "eat",
    "eating",
    "serve",
    "served",
    "dining",
    "dish",
    "dishes",
    "item",
    "items",
    "price",
    "prices",
    "breakfast",
    "lunch",
    "dinner",
    "ird",
}
_CATALOG_CHUNK_MARKERS = (
    "menu_name",
    "section_name",
    "item_name",
    "price_inr",
    "categorization",
    "ingredients",
    "in-room_dining",
    "in room dining",
    "ird",
    "breakfast",
    "lunch",
    "dinner",
)
_POLICY_QUERY_MARKERS = (
    "check in",
    "check-in",
    "checkin",
    "check out",
    "check-out",
    "checkout",
    "timing",
    "timings",
    "hours",
    "policy",
    "arrival",
    "departure",
)
_QUERY_NORMALIZATION_REPLACEMENTS = {
    "whatis": "what is",
    "whats": "what is",
    "timings": "timing",
    "timng": "timing",
    "timming": "timing",
    "timmings": "timing",
    "availabke": "available",
    "availble": "available",
    "avaiable": "available",
    "chcek": "check",
    "chek": "check",
    "chec": "check",
    "checkin": "check in",
    "checkout": "check out",
    "inroom": "in room",
    "in-room": "in room",
}


@dataclass
class RetrievedChunk:
    source: str
    content: str
    score: float
    chunk_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RAGAnswer:
    answer: str
    confidence: float
    sources: list[str]
    trace_id: str = ""


class RAGService:
    """Retrieval + generation service used by FAQ/knowledge handlers."""

    def __init__(
        self,
        kb_dir: Optional[Path] = None,
        local_index_file: Optional[Path] = None,
        backend: Optional[str] = None,
    ):
        self.kb_dir = Path(kb_dir or DEFAULT_KB_DIR)
        self.local_index_file = Path(local_index_file or settings.rag_local_index_file)
        self.backend = str(backend or settings.rag_backend).strip().lower()
        if self.backend not in {"local", "qdrant"}:
            self.backend = "local"

        self.chunk_size = max(40, int(settings.rag_chunk_size))
        self.chunk_overlap = max(0, min(int(settings.rag_chunk_overlap), self.chunk_size - 1))
        self.top_k = max(1, int(settings.rag_top_k))
        self.min_retrieval_score = float(settings.rag_min_retrieval_score)
        self.enable_rerank = bool(settings.rag_enable_rerank)
        self.enable_mmr = bool(getattr(settings, "rag_enable_mmr", True))
        self.mmr_lambda = min(1.0, max(0.0, float(getattr(settings, "rag_mmr_lambda", 0.7))))
        self.candidate_pool_min = max(1, int(getattr(settings, "rag_candidate_pool_min", 20)))
        self.candidate_pool_max = max(self.candidate_pool_min, int(getattr(settings, "rag_candidate_pool_max", 40)))
        self.enable_llm_query_rewrite = bool(getattr(settings, "rag_enable_llm_query_rewrite", True))
        self.llm_query_rewrite_max_tokens = max(16, int(getattr(settings, "rag_llm_query_rewrite_max_tokens", 64)))
        self.enable_llm_rerank = bool(getattr(settings, "rag_enable_llm_rerank", False))
        self.step_logs_enabled = bool(getattr(settings, "rag_step_logs_enabled", True))
        self.step_log_file = Path(getattr(settings, "rag_step_log_file", "./logs/detailedsteps.log"))
        self.step_log_preview_chars = max(80, int(getattr(settings, "rag_step_log_preview_chars", 260)))

        self._qdrant_client: Optional[QdrantClient] = None

    @staticmethod
    def _normalize_tenant(value: str) -> str:
        return str(value or "default").strip().lower().replace(" ", "_")

    def _new_trace(
        self,
        question: str,
        tenant_id: str,
        business_type: str,
    ) -> Optional[dict[str, Any]]:
        if not self.step_logs_enabled:
            return None
        return {
            "trace_id": f"rag-{uuid.uuid4().hex[:12]}",
            "started_at": datetime.now(UTC).isoformat(),
            "tenant_id": self._normalize_tenant(tenant_id),
            "business_type": str(business_type or "generic"),
            "question": re.sub(r"\s+", " ", str(question or "").strip()),
            "backend": self.backend,
            "steps": [],
        }

    def _preview_text(self, value: Any) -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip())
        if len(text) <= self.step_log_preview_chars:
            return text
        return text[: self.step_log_preview_chars] + "..."

    def _summarize_chunks(self, chunks: list[RetrievedChunk], limit: int = 5) -> list[dict[str, Any]]:
        summary: list[dict[str, Any]] = []
        for chunk in chunks[: max(1, limit)]:
            summary.append(
                {
                    "source": chunk.source,
                    "chunk_id": chunk.chunk_id,
                    "score": round(float(chunk.score), 4),
                    "section": str((chunk.metadata or {}).get("section") or ""),
                    "preview": self._preview_text(chunk.content),
                }
            )
        return summary

    def _trace_step(
        self,
        trace: Optional[dict[str, Any]],
        step: str,
        status: str,
        input_data: Optional[dict[str, Any]] = None,
        output_data: Optional[dict[str, Any]] = None,
        error: str = "",
    ) -> None:
        if not self.step_logs_enabled or trace is None:
            return
        entry: dict[str, Any] = {
            "time": datetime.now(UTC).isoformat(),
            "step": step,
            "status": status,
        }
        if input_data:
            entry["input"] = input_data
        if output_data:
            entry["output"] = output_data
        if error:
            entry["error"] = self._preview_text(error)
        trace.setdefault("steps", []).append(entry)

    def _write_trace(self, trace: Optional[dict[str, Any]], final_status: str) -> None:
        if not self.step_logs_enabled or trace is None:
            return
        trace["completed_at"] = datetime.now(UTC).isoformat()
        trace["final_status"] = str(final_status or "unknown")
        try:
            self.step_log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.step_log_file.open("a", encoding="utf-8") as fh:
                fh.write(self._render_human_trace(trace))
                fh.write(f"--- RAG TRACE {trace.get('trace_id', '')} ---\n")
                fh.write(json.dumps(trace, ensure_ascii=False, indent=2))
                fh.write("\n--- END RAG TRACE ---\n")
        except Exception:
            # Logging must never affect runtime behavior.
            return

    @staticmethod
    def _get_step(trace: dict[str, Any], step_name: str) -> Optional[dict[str, Any]]:
        for step in trace.get("steps", []):
            if isinstance(step, dict) and step.get("step") == step_name:
                return step
        return None

    def _render_human_trace(self, trace: dict[str, Any]) -> str:
        trace_id = str(trace.get("trace_id") or "")
        started_at = str(trace.get("started_at") or "")
        completed_at = str(trace.get("completed_at") or "")
        final_status = str(trace.get("final_status") or "unknown")
        tenant_id = str(trace.get("tenant_id") or "")

        rewrite_step = self._get_step(trace, "rewrite_query_llm") or {}
        rewrite_status = str(rewrite_step.get("status") or "unknown")
        rewrite_output = rewrite_step.get("output", {}) if isinstance(rewrite_step.get("output"), dict) else {}
        rewritten_query = str(rewrite_output.get("rewritten_query") or trace.get("question") or "")

        normalize_step = self._get_step(trace, "normalize_query") or {}
        normalize_output = normalize_step.get("output", {}) if isinstance(normalize_step.get("output"), dict) else {}
        retrieval_query = str(normalize_output.get("normalized_query") or rewritten_query or trace.get("question") or "")

        retrieve_step = self._get_step(trace, "retrieve_complete") or {}
        retrieve_output = retrieve_step.get("output", {}) if isinstance(retrieve_step.get("output"), dict) else {}
        final_chunks = retrieve_output.get("final_chunks", [])
        if not isinstance(final_chunks, list):
            final_chunks = []

        context_step = self._get_step(trace, "context_build") or {}
        context_output = context_step.get("output", {}) if isinstance(context_step.get("output"), dict) else {}
        source_list = context_output.get("sources", [])
        if not isinstance(source_list, list):
            source_list = []

        answer_step = self._get_step(trace, "generate_answer_llm") or self._get_step(trace, "generate_answer_fallback") or {}
        answer_status = str(answer_step.get("status") or "unknown")
        answer_output = answer_step.get("output", {}) if isinstance(answer_step.get("output"), dict) else {}
        answer_preview = str(answer_output.get("answer_preview") or "")
        llm_input = answer_step.get("input", {}) if isinstance(answer_step.get("input"), dict) else {}
        llm_question = str(llm_input.get("question_for_llm") or trace.get("question") or "")
        llm_context_preview = str(llm_input.get("context_preview") or "")

        lines: list[str] = []
        lines.append(f"===== RAG FLOW TRACE {trace_id} =====")
        lines.append(f"Tenant: {tenant_id} | Status: {final_status}")
        lines.append(f"Started: {started_at} | Completed: {completed_at}")
        lines.append("")
        lines.append("1) USER QUERY")
        lines.append(f"   {self._preview_text(trace.get('question', ''))}")
        lines.append("")
        lines.append(f"2) REWRITTEN QUERY ({rewrite_status})")
        lines.append(f"   {self._preview_text(rewritten_query)}")
        lines.append("")
        lines.append("3) RAG OUTPUT (selected chunks)")
        if final_chunks:
            for idx, chunk in enumerate(final_chunks[:5], start=1):
                if not isinstance(chunk, dict):
                    continue
                source = str(chunk.get("source") or "unknown")
                chunk_id = str(chunk.get("chunk_id") or "")
                score = chunk.get("score")
                preview = self._preview_text(chunk.get("preview") or "")
                lines.append(f"   {idx}. [{score}] {source}#{chunk_id} -> {preview}")
        else:
            lines.append("   No chunks selected.")
        lines.append("")
        lines.append("4) INPUT SENT TO LLM")
        lines.append(f"   Retrieval query used: {self._preview_text(retrieval_query)}")
        lines.append(f"   Question for LLM: {self._preview_text(llm_question)}")
        lines.append(f"   Sources: {', '.join(source_list[:8]) if source_list else 'None'}")
        if llm_context_preview:
            lines.append(f"   Context preview: {self._preview_text(llm_context_preview)}")
        lines.append("")
        lines.append(f"5) LLM OUTPUT ({answer_status})")
        lines.append(f"   {self._preview_text(answer_preview)}")
        lines.append("===== END RAG FLOW TRACE =====")
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return set(re.findall(r"[a-z0-9]+", text.lower()))

    @staticmethod
    def _expand_tokens(tokens: set[str]) -> set[str]:
        """
        Expand lexical variants so common fused forms still match reliably.
        """
        expanded = set(tokens)
        for token in list(tokens):
            clean = str(token or "").strip().lower()
            if not clean:
                continue
            compact = clean.replace("_", "").replace("-", "")
            if compact.startswith("checkin"):
                expanded.update({"checkin", "check", "in"})
            if compact.startswith("checkout"):
                expanded.update({"checkout", "check", "out"})
            if compact == "timings":
                expanded.update({"timing", "time"})
            if compact in {"timing", "hours", "hour"}:
                expanded.add("time")
        return expanded

    @staticmethod
    def _normalize_query_text(question: str) -> str:
        value = re.sub(r"\s+", " ", str(question or "").strip().lower())
        if not value:
            return ""

        # Handle common fused forms first.
        value = re.sub(r"\bcheck[\s_-]*in\b", "check in", value)
        value = re.sub(r"\bcheck[\s_-]*out\b", "check out", value)
        value = re.sub(r"\bch(?:e|a)?ck[\s_-]*in\b", "check in", value)
        value = re.sub(r"\bch(?:e|a)?ck[\s_-]*out\b", "check out", value)

        for typo, replacement in _QUERY_NORMALIZATION_REPLACEMENTS.items():
            value = re.sub(rf"\b{re.escape(typo)}\b", replacement, value)

        value = re.sub(r"\s+", " ", value).strip()
        return value

    def _build_query_variants(self, question: str) -> list[str]:
        """
        Build a small set of normalized retrieval queries for typo/variant robustness
        without changing user-visible behavior.
        """
        base = re.sub(r"\s+", " ", str(question or "").strip())
        if not base:
            return []

        normalized = self._normalize_query_text(base)
        variants: list[str] = [base]
        if normalized and normalized != base.lower():
            variants.append(normalized)

        normalized_lower = normalized or base.lower()
        if "ird" in normalized_lower and "in room dining" not in normalized_lower:
            variants.append(normalized_lower.replace("ird", "in room dining"))
        if "in room dining" in normalized_lower and "menu" not in normalized_lower:
            variants.append(f"{normalized_lower} menu")

        if ("check in" in normalized_lower or "check out" in normalized_lower) and not any(
            marker in normalized_lower for marker in ("time", "timing", "hours", "policy")
        ):
            variants.append(f"{normalized_lower} timing policy")

        deduped: list[str] = []
        seen: set[str] = set()
        for query in variants:
            normalized_query = re.sub(r"\s+", " ", str(query or "").strip().lower())
            if not normalized_query or normalized_query in seen:
                continue
            seen.add(normalized_query)
            deduped.append(re.sub(r"\s+", " ", str(query or "").strip()))
        return deduped[:3]

    def _is_reasonable_query_rewrite(self, original: str, rewritten: str) -> bool:
        base = str(original or "").strip().lower()
        candidate = str(rewritten or "").strip().lower()
        if not base or not candidate:
            return False

        if candidate == base:
            return True
        if len(candidate) > 240:
            return False
        if candidate.startswith("i'm having trouble processing"):
            return False

        base_tokens = self._tokenize(base)
        candidate_tokens = self._tokenize(candidate)
        if not base_tokens or not candidate_tokens:
            return False

        overlap = len(base_tokens & candidate_tokens) / max(1, len(base_tokens))
        return overlap >= 0.45

    async def _rewrite_query_with_llm(
        self,
        question: str,
        business_type: str = "generic",
    ) -> str:
        """
        LLM-assisted typo/spelling normalization before retrieval.
        Preserves intent, entities, and constraints; never invents new asks.
        """
        original = re.sub(r"\s+", " ", str(question or "").strip())
        if not original:
            return ""
        if not self.enable_llm_query_rewrite or not settings.openai_api_key:
            return original

        prompt = (
            "You normalize user queries for retrieval.\n"
            "Correct spelling/typos and minor grammar only.\n"
            "Do NOT add new intent, entities, dates, names, times, or policies.\n"
            "Keep domain terms (menu names, service names, brands) intact.\n"
            "Return exactly one rewritten query line and nothing else."
        )
        try:
            rewritten = await llm_client.chat(
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": f"Business type: {business_type}\nQuery: {original}"},
                ],
                temperature=0.0,
                max_tokens=self.llm_query_rewrite_max_tokens,
            )
        except Exception:
            return original

        candidate = re.sub(r"\s+", " ", str(rewritten or "").strip().strip('"').strip("'"))
        if not candidate:
            return original
        if self._is_reasonable_query_rewrite(original, candidate):
            return candidate
        return original

    def _candidate_pool_size(self, final_k: int) -> int:
        requested = max(self.candidate_pool_min, final_k * 5)
        return min(self.candidate_pool_max, requested)

    def _chunk_token_set(self, chunk: RetrievedChunk) -> set[str]:
        tokens = self._expand_tokens(self._tokenize(chunk.content))
        return {
            token
            for token in tokens
            if token and token not in _QUERY_STOPWORDS and len(token) > 1
        }

    @staticmethod
    def _token_jaccard_similarity(tokens_a: set[str], tokens_b: set[str]) -> float:
        if not tokens_a or not tokens_b:
            return 0.0
        union = tokens_a | tokens_b
        if not union:
            return 0.0
        return len(tokens_a & tokens_b) / len(union)

    def _select_chunks_mmr(
        self,
        question: str,
        chunks: list[RetrievedChunk],
        top_k: int,
        lambda_value: float,
    ) -> list[RetrievedChunk]:
        if not chunks:
            return []

        limit = max(1, min(top_k, len(chunks)))
        mmr_lambda = min(1.0, max(0.0, float(lambda_value)))

        relevance_scores: list[float] = []
        token_sets: list[set[str]] = []
        for chunk in chunks:
            lexical = self._candidate_score(
                question,
                chunk.content,
                chunk.metadata if isinstance(chunk.metadata, dict) else None,
            )
            relevance = (0.7 * float(chunk.score)) + (0.3 * lexical)
            relevance_scores.append(max(0.0, relevance))
            token_sets.append(self._chunk_token_set(chunk))

        candidate_indices = list(range(len(chunks)))
        selected_indices: list[int] = []

        while candidate_indices and len(selected_indices) < limit:
            best_idx: Optional[int] = None
            best_mmr = -1e9
            best_relevance = -1.0

            for idx in candidate_indices:
                relevance = relevance_scores[idx]
                if selected_indices:
                    max_similarity = max(
                        self._token_jaccard_similarity(token_sets[idx], token_sets[selected_idx])
                        for selected_idx in selected_indices
                    )
                else:
                    max_similarity = 0.0

                mmr_score = (mmr_lambda * relevance) - ((1.0 - mmr_lambda) * max_similarity)
                if mmr_score > best_mmr or (abs(mmr_score - best_mmr) <= 1e-9 and relevance > best_relevance):
                    best_idx = idx
                    best_mmr = mmr_score
                    best_relevance = relevance

            if best_idx is None:
                break

            selected_indices.append(best_idx)
            candidate_indices.remove(best_idx)

        selected_chunks: list[RetrievedChunk] = []
        for idx in selected_indices:
            chunk = chunks[idx]
            selected_chunks.append(
                RetrievedChunk(
                    source=chunk.source,
                    content=chunk.content,
                    score=relevance_scores[idx],
                    chunk_id=chunk.chunk_id,
                    metadata=chunk.metadata,
                )
            )
        return selected_chunks

    async def _llm_rerank(
        self,
        question: str,
        chunks: list[RetrievedChunk],
        trace: Optional[dict[str, Any]] = None,
    ) -> list[RetrievedChunk]:
        """
        Optional LLM rerank stage. Disabled by default for backward compatibility.
        """
        if not chunks:
            self._trace_step(
                trace,
                step="llm_rerank",
                status="skipped",
                output_data={"reason": "no_chunks"},
            )
            return []
        if not self.enable_llm_rerank or not settings.openai_api_key:
            self._trace_step(
                trace,
                step="llm_rerank",
                status="skipped",
                output_data={
                    "reason": "disabled_or_missing_api_key",
                    "enabled": self.enable_llm_rerank,
                },
            )
            return chunks

        shortlist = chunks[: min(len(chunks), 12)]
        lines: list[str] = []
        for idx, chunk in enumerate(shortlist, start=1):
            snippet = re.sub(r"\s+", " ", chunk.content).strip()[:300]
            label = f"{chunk.source}#{chunk.chunk_id}" if chunk.chunk_id else chunk.source
            lines.append(f"{idx}. [{label}] {snippet}")

        prompt = (
            "You are ranking retrieval chunks for answer grounding.\n"
            "Given the user question and chunk list, return ONLY the best chunk numbers in priority order "
            "(comma separated, for example: 2,1,4). Do not include extra text.\n\n"
            f"Question: {question}\n\nChunks:\n" + "\n".join(lines)
        )
        try:
            response = await llm_client.chat(
                messages=[{"role": "system", "content": prompt}],
                temperature=0.0,
                max_tokens=80,
            )
        except Exception:
            self._trace_step(
                trace,
                step="llm_rerank",
                status="failed",
                error="llm rerank request failed",
            )
            return chunks

        ranked_numbers: list[int] = []
        for raw in re.findall(r"\d+", str(response or "")):
            idx = int(raw)
            if 1 <= idx <= len(shortlist) and idx not in ranked_numbers:
                ranked_numbers.append(idx)

        if not ranked_numbers:
            self._trace_step(
                trace,
                step="llm_rerank",
                status="no_change",
                output_data={"reason": "llm_response_unparsable"},
            )
            return chunks

        reordered: list[RetrievedChunk] = [shortlist[idx - 1] for idx in ranked_numbers]
        for chunk in shortlist:
            if chunk not in reordered:
                reordered.append(chunk)

        if len(chunks) > len(shortlist):
            reordered.extend(chunks[len(shortlist):])
        self._trace_step(
            trace,
            step="llm_rerank",
            status="success",
            input_data={"question": self._preview_text(question), "ranked_numbers": ranked_numbers},
            output_data={"reranked_chunks": self._summarize_chunks(reordered, limit=min(len(reordered), 6))},
        )
        return reordered

    @staticmethod
    def _normalize_text_block(text: str) -> str:
        value = str(text or "")
        value = value.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
        value = value.replace("\r\n", "\n")
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()

    def _stringify_kb_value(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return self._normalize_text_block(json.dumps(value, ensure_ascii=False, indent=2))
        return self._normalize_text_block(str(value))

    def _render_structured_document(self, payload: Any, source_name: str) -> Optional[str]:
        """
        Convert wrapped JSON knowledge payloads into sectioned plain text for retrieval.
        Handles common admin export format:
        {"data":"{\"editable\": {...}}", ...}
        """
        if not isinstance(payload, dict):
            return None

        editable: Optional[dict[str, Any]] = None
        if isinstance(payload.get("editable"), dict):
            editable = payload.get("editable")
        elif isinstance(payload.get("data"), str):
            try:
                inner = json.loads(payload["data"])
            except Exception:
                inner = None
            if isinstance(inner, dict):
                if isinstance(inner.get("editable"), dict):
                    editable = inner.get("editable")
                else:
                    editable = inner

        if not isinstance(editable, dict) or not editable:
            return None

        lines: list[str] = [f"# Source: {source_name}", "## Business Knowledge"]
        for raw_key, raw_value in editable.items():
            section_title = re.sub(r"[_\-]+", " ", str(raw_key or "").strip()).strip()
            section_title = re.sub(r"\s+", " ", section_title)
            if not section_title:
                continue
            body = self._stringify_kb_value(raw_value)
            if not body:
                continue
            lines.append(f"### {section_title}")
            lines.append(body)
            lines.append("")

        result = "\n".join(lines).strip()
        return result or None

    def _chunk_text(self, text: str) -> list[str]:
        words = re.findall(r"\S+", text)
        if not words:
            return []

        chunks: list[str] = []
        step = max(1, self.chunk_size - self.chunk_overlap)
        for start in range(0, len(words), step):
            block = words[start : start + self.chunk_size]
            if not block:
                break
            chunks.append(" ".join(block))
            if start + self.chunk_size >= len(words):
                break
        return chunks

    def _iter_kb_files(self) -> list[Path]:
        self.kb_dir.mkdir(parents=True, exist_ok=True)
        files: list[Path] = []
        uploads_root = self.kb_dir / "uploads"
        if uploads_root.exists() and uploads_root.is_dir():
            for ext in ("*.md", "*.txt", "*.markdown", "*.rst", "*.json"):
                files.extend(uploads_root.rglob(ext))
        deduped: list[Path] = []
        seen: set[str] = set()
        for path in files:
            marker = str(path.resolve()) if path.exists() else str(path)
            if marker in seen:
                continue
            seen.add(marker)
            deduped.append(path)
        deduped.sort()
        return deduped

    def _read_document(self, path: Path) -> Optional[str]:
        try:
            raw = path.read_text(encoding="utf-8")
            suffix = path.suffix.lower()

            if suffix == ".json":
                payload = json.loads(raw)
                structured = self._render_structured_document(payload, path.name)
                if structured:
                    return structured
                return self._normalize_text_block(json.dumps(payload, ensure_ascii=False, indent=2))

            stripped = raw.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                try:
                    payload = json.loads(stripped)
                except Exception:
                    payload = None
                structured = self._render_structured_document(payload, path.name)
                if structured:
                    return structured

            return self._normalize_text_block(raw)
        except Exception:
            return None

    @staticmethod
    def _split_into_sections(text: str) -> list[tuple[str, str]]:
        """
        Split documents into semantic sections before chunking.
        """
        normalized = str(text or "").strip()
        if not normalized:
            return []

        sections: list[tuple[str, str]] = []
        current_title = "document"
        current_lines: list[str] = []
        heading_re = re.compile(r"^\s*#{2,4}\s+(.+?)\s*$")

        for line in normalized.splitlines():
            heading = heading_re.match(line)
            if heading:
                block = "\n".join(current_lines).strip()
                if block:
                    sections.append((current_title, block))
                current_title = heading.group(1).strip()
                current_lines = []
                continue
            current_lines.append(line)

        trailing = "\n".join(current_lines).strip()
        if trailing:
            sections.append((current_title, trailing))

        if not sections:
            return [("document", normalized)]
        return sections

    def _load_documents(self, file_paths: Optional[list[str]] = None) -> list[tuple[str, str]]:
        docs: list[tuple[str, str]] = []
        if file_paths:
            files = [Path(p) for p in file_paths]
        else:
            files = self._iter_kb_files()

        for file in files:
            text = self._read_document(file)
            if not text:
                continue
            source_name = file.name if file.is_file() else str(file)
            docs.append((source_name, text))
        return docs

    @staticmethod
    def _chunk_hash(tenant_id: str, source: str, chunk_index: int, content: str) -> str:
        payload = f"{tenant_id}|{source}|{chunk_index}|{content}".encode("utf-8")
        return hashlib.sha1(payload).hexdigest()[:32]

    def _build_chunk_rows(
        self,
        documents: list[tuple[str, str]],
        tenant_id: str,
        business_type: str = "generic",
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        normalized_tenant = self._normalize_tenant(tenant_id)
        for source, text in documents:
            chunk_counter = 0
            sections = self._split_into_sections(text)
            for section_title, section_text in sections:
                chunks = self._chunk_text(section_text)
                for chunk in chunks:
                    section_value = str(section_title or "").strip()
                    content = chunk
                    if section_value and section_value.lower() not in {"document", "business knowledge"}:
                        content = f"Section: {section_value}\n{chunk}"

                    row = {
                        "id": self._chunk_hash(normalized_tenant, source, chunk_counter, content),
                        "tenant_id": normalized_tenant,
                        "business_type": str(business_type or "generic").strip().lower(),
                        "source": source,
                        "chunk_index": chunk_counter,
                        "chunk_id": f"{source}:{chunk_counter}",
                        "section": section_value,
                        "content": content,
                    }
                    rows.append(row)
                    chunk_counter += 1
        return rows

    def _load_local_index(self) -> list[dict[str, Any]]:
        if not self.local_index_file.exists():
            return []
        try:
            payload = json.loads(self.local_index_file.read_text(encoding="utf-8"))
        except Exception:
            return []
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        return []

    def _save_local_index(self, rows: list[dict[str, Any]]) -> None:
        self.local_index_file.parent.mkdir(parents=True, exist_ok=True)
        self.local_index_file.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _is_qdrant_ready(self) -> bool:
        return bool(
            self.backend == "qdrant"
            and QdrantClient is not None
            and qdrant_models is not None
            and settings.qdrant_url
            and settings.openai_api_key
        )

    def _get_qdrant_client(self) -> Optional[QdrantClient]:
        if not self._is_qdrant_ready():
            return None
        if self._qdrant_client is None:
            self._qdrant_client = QdrantClient(
                url=settings.qdrant_url,
                api_key=settings.qdrant_api_key or None,
                timeout=30.0,
            )
        return self._qdrant_client

    async def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = await llm_client.client.embeddings.create(
            model=settings.openai_embedding_model,
            input=texts,
        )
        return [item.embedding for item in response.data]

    def _ensure_qdrant_collection(self, client: QdrantClient) -> None:
        collection = settings.qdrant_collection
        exists = False
        try:
            exists = client.collection_exists(collection)
        except Exception:
            # Fallback for older client versions
            collections = client.get_collections()
            names = {item.name for item in getattr(collections, "collections", [])}
            exists = collection in names

        if exists:
            return

        client.create_collection(
            collection_name=collection,
            vectors_config=qdrant_models.VectorParams(
                size=int(settings.qdrant_vector_size),
                distance=qdrant_models.Distance.COSINE,
            ),
        )

    async def _upsert_qdrant(
        self,
        rows: list[dict[str, Any]],
        tenant_id: str,
        clear_existing: bool,
    ) -> tuple[bool, str]:
        client = self._get_qdrant_client()
        if client is None:
            return False, "qdrant_not_configured"

        self._ensure_qdrant_collection(client)
        collection = settings.qdrant_collection
        normalized_tenant = self._normalize_tenant(tenant_id)

        if clear_existing:
            client.delete(
                collection_name=collection,
                points_selector=qdrant_models.FilterSelector(
                    filter=qdrant_models.Filter(
                        must=[
                            qdrant_models.FieldCondition(
                                key="tenant_id",
                                match=qdrant_models.MatchValue(value=normalized_tenant),
                            )
                        ]
                    )
                ),
                wait=True,
            )

        batch_size = 64
        for i in range(0, len(rows), batch_size):
            batch = rows[i : i + batch_size]
            vectors = await self._embed_texts([row["content"] for row in batch])
            points = []
            for row, vector in zip(batch, vectors):
                points.append(
                    qdrant_models.PointStruct(
                        id=row["id"],
                        vector=vector,
                        payload={
                            "tenant_id": row["tenant_id"],
                            "business_type": row["business_type"],
                            "source": row["source"],
                            "chunk_index": row["chunk_index"],
                            "chunk_id": row["chunk_id"],
                            "section": row.get("section", ""),
                            "content": row["content"],
                        },
                    )
                )
            client.upsert(
                collection_name=collection,
                points=points,
                wait=True,
            )

        return True, "ok"

    async def ingest_documents(
        self,
        documents: list[tuple[str, str]],
        tenant_id: str = "default",
        business_type: str = "generic",
        clear_existing: bool = False,
    ) -> dict[str, Any]:
        normalized_tenant = self._normalize_tenant(tenant_id)
        rows = self._build_chunk_rows(documents, normalized_tenant, business_type)

        existing_rows = self._load_local_index()
        if clear_existing:
            existing_rows = [r for r in existing_rows if r.get("tenant_id") != normalized_tenant]
        merged_rows = {row["id"]: row for row in existing_rows}
        for row in rows:
            merged_rows[row["id"]] = row
        self._save_local_index(list(merged_rows.values()))

        backend_used = "local"
        qdrant_status = "skipped"
        if self.backend == "qdrant":
            ok, qdrant_status = await self._upsert_qdrant(
                rows=rows,
                tenant_id=normalized_tenant,
                clear_existing=clear_existing,
            )
            if ok:
                backend_used = "qdrant"

        return {
            "tenant_id": normalized_tenant,
            "documents_ingested": len(documents),
            "chunks_indexed": len(rows),
            "backend_used": backend_used,
            "qdrant_status": qdrant_status,
            "local_index_file": str(self.local_index_file),
        }

    async def ingest_from_knowledge_base(
        self,
        tenant_id: str = "default",
        business_type: str = "generic",
        clear_existing: bool = False,
        file_paths: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        docs = self._load_documents(file_paths=file_paths)
        report = await self.ingest_documents(
            documents=docs,
            tenant_id=tenant_id,
            business_type=business_type,
            clear_existing=clear_existing,
        )
        report["kb_dir"] = str(self.kb_dir)
        report["files"] = [source for source, _ in docs]
        return report

    def _candidate_score(self, question: str, content: str, row: Optional[dict[str, Any]] = None) -> float:
        q_tokens = {
            token for token in self._tokenize(question)
            if token and token not in _QUERY_STOPWORDS
        }
        q_tokens = self._expand_tokens(q_tokens)
        c_tokens = self._expand_tokens(self._tokenize(content))
        if not c_tokens or not q_tokens:
            return 0.0
        overlap_count = len(q_tokens & c_tokens)
        overlap = overlap_count / max(1, len(q_tokens))
        score = overlap

        question_lower = str(question or "").lower()
        content_lower = str(content or "").lower()
        section_lower = str((row or {}).get("section") or "").lower()
        is_policy_query = any(marker in question_lower for marker in _POLICY_QUERY_MARKERS)

        # Penalize weak lexical matches for multi-token questions.
        if len(q_tokens) >= 3 and overlap_count <= 1:
            score *= 0.45

        is_catalog_query = bool(
            _CATALOG_QUERY_TERMS & self._tokenize(question_lower)
            or "in room dining" in question_lower
            or "in-room dining" in question_lower
        )
        is_catalog_chunk = any(marker in content_lower for marker in _CATALOG_CHUNK_MARKERS) or any(
            marker in section_lower for marker in _CATALOG_CHUNK_MARKERS
        )

        if is_catalog_query:
            if is_catalog_chunk:
                score += 0.25
            else:
                score *= 0.65

            structured_hits = sum(1 for marker in ("item_name", "price_inr", "section_name", "menu_name") if marker in content_lower)
            score += min(0.2, structured_hits * 0.06)

            if (
                "in room dining" in question_lower
                or "in-room dining" in question_lower
                or "ird" in self._tokenize(question_lower)
            ):
                if (
                    "in room dining" in content_lower
                    or "in-room dining" in content_lower
                    or "ird" in content_lower
                    or "in room dining" in section_lower
                    or "ird" in section_lower
                ):
                    score += 0.45

        if section_lower:
            section_tokens = self._tokenize(section_lower)
            if section_tokens:
                section_overlap = len(q_tokens & section_tokens) / max(1, len(q_tokens))
                score += section_overlap * 0.3

        if is_policy_query:
            policy_section_markers = ("time", "timing", "hours", "policy", "checkin", "checkout", "arrival", "departure")
            if any(marker in section_lower for marker in policy_section_markers):
                score += 0.22
            elif any(marker in content_lower for marker in ("check-in time", "check-out time", "checkin time", "checkout time")):
                score += 0.16

        return max(0.0, score)

    @staticmethod
    def _dedupe_chunks(chunks: list[RetrievedChunk], limit: int) -> list[RetrievedChunk]:
        if not chunks:
            return []
        deduped: list[RetrievedChunk] = []
        seen: set[tuple[str, str]] = set()
        for chunk in sorted(chunks, key=lambda c: c.score, reverse=True):
            section = str((chunk.metadata or {}).get("section") or "").strip().lower()
            signature = re.sub(r"\s+", " ", str(chunk.content or "").strip().lower())[:260]
            key = (section, signature)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(chunk)
            if len(deduped) >= limit:
                break
        return deduped

    def _retrieve_local(
        self,
        question: str,
        tenant_id: str,
        top_k: int,
    ) -> list[RetrievedChunk]:
        rows = self._load_local_index()
        normalized_tenant = self._normalize_tenant(tenant_id)
        scoped = [row for row in rows if row.get("tenant_id") == normalized_tenant]

        scored: list[RetrievedChunk] = []
        for row in scoped:
            content = str(row.get("content", ""))
            score = self._candidate_score(question, content, row)
            if score <= 0:
                continue
            scored.append(
                RetrievedChunk(
                    source=str(row.get("source", "unknown")),
                    content=content,
                    score=score,
                    chunk_id=str(row.get("chunk_id", "")),
                    metadata={
                        "tenant_id": row.get("tenant_id"),
                        "business_type": row.get("business_type"),
                        "section": row.get("section"),
                    },
                )
            )

        scored.sort(key=lambda c: c.score, reverse=True)
        return scored[:top_k]

    async def _retrieve_qdrant(
        self,
        question: str,
        tenant_id: str,
        top_k: int,
    ) -> list[RetrievedChunk]:
        client = self._get_qdrant_client()
        if client is None:
            return []

        vector = (await self._embed_texts([question]))[0]
        normalized_tenant = self._normalize_tenant(tenant_id)

        def _search(for_tenant: str):
            return client.search(
                collection_name=settings.qdrant_collection,
                query_vector=vector,
                query_filter=qdrant_models.Filter(
                    must=[
                        qdrant_models.FieldCondition(
                            key="tenant_id",
                            match=qdrant_models.MatchValue(value=for_tenant),
                        )
                    ]
                ),
                limit=top_k,
            )

        results = _search(normalized_tenant)

        chunks: list[RetrievedChunk] = []
        for item in results:
            payload = item.payload or {}
            content = str(payload.get("content") or "")
            if not content:
                continue
            chunks.append(
                RetrievedChunk(
                    source=str(payload.get("source") or "unknown"),
                    content=content,
                    score=float(item.score),
                    chunk_id=str(payload.get("chunk_id") or ""),
                    metadata={
                        "tenant_id": payload.get("tenant_id"),
                        "business_type": payload.get("business_type"),
                        "section": payload.get("section"),
                    },
                )
            )
        return chunks

    def _rerank(self, question: str, chunks: list[RetrievedChunk], top_k: int) -> list[RetrievedChunk]:
        if not chunks:
            return []

        rescored: list[RetrievedChunk] = []
        for chunk in chunks:
            lexical = self._candidate_score(
                question,
                chunk.content,
                chunk.metadata if isinstance(chunk.metadata, dict) else None,
            )
            combined = (0.7 * chunk.score) + (0.3 * lexical)
            rescored.append(
                RetrievedChunk(
                    source=chunk.source,
                    content=chunk.content,
                    score=combined,
                    chunk_id=chunk.chunk_id,
                    metadata=chunk.metadata,
                )
            )
        rescored.sort(key=lambda c: c.score, reverse=True)
        return rescored[:top_k]

    async def retrieve(
        self,
        question: str,
        tenant_id: str = "default",
        top_k: Optional[int] = None,
        trace: Optional[dict[str, Any]] = None,
        property_filter: Optional[str] = None,
    ) -> list[RetrievedChunk]:
        limit = max(1, int(top_k or self.top_k))
        candidate_limit = self._candidate_pool_size(limit)

        query_variants = self._build_query_variants(question) or [question]
        self._trace_step(
            trace,
            step="retrieve_start",
            status="success",
            input_data={
                "question": self._preview_text(question),
                "tenant_id": self._normalize_tenant(tenant_id),
                "requested_top_k": limit,
            },
            output_data={
                "candidate_pool_size": candidate_limit,
                "query_variants": query_variants,
            },
        )
        aggregated_chunks: list[RetrievedChunk] = []

        for idx, query_variant in enumerate(query_variants):
            variant_chunks: list[RetrievedChunk] = []
            backend_used = "local"
            if self._is_qdrant_ready():
                try:
                    variant_chunks = await self._retrieve_qdrant(
                        query_variant,
                        tenant_id=tenant_id,
                        top_k=candidate_limit,
                    )
                    if variant_chunks:
                        backend_used = "qdrant"
                except Exception:
                    self._trace_step(
                        trace,
                        step="retrieve_variant_qdrant",
                        status="failed",
                        input_data={"variant_index": idx, "query_variant": query_variant},
                        error="qdrant retrieval failed; falling back to local index",
                    )
                    variant_chunks = []

            if not variant_chunks:
                variant_chunks = self._retrieve_local(
                    query_variant,
                    tenant_id=tenant_id,
                    top_k=candidate_limit,
                )
                backend_used = "local"

            weight = max(0.9, 1.0 - (0.04 * idx))
            self._trace_step(
                trace,
                step="retrieve_variant",
                status="success",
                input_data={
                    "variant_index": idx,
                    "query_variant": query_variant,
                    "backend": backend_used,
                    "weight": round(weight, 3),
                },
                output_data={
                    "chunks_retrieved": len(variant_chunks),
                    "top_chunks": self._summarize_chunks(variant_chunks, limit=3),
                },
            )
            for chunk in variant_chunks:
                aggregated_chunks.append(
                    RetrievedChunk(
                        source=chunk.source,
                        content=chunk.content,
                        score=float(chunk.score) * weight,
                        chunk_id=chunk.chunk_id,
                        metadata=chunk.metadata,
                    )
                )

        self._trace_step(
            trace,
            step="aggregate_candidates",
            status="success",
            output_data={"aggregated_count": len(aggregated_chunks)},
        )
        chunks = self._dedupe_chunks(aggregated_chunks, limit=max(candidate_limit * 2, candidate_limit))
        self._trace_step(
            trace,
            step="dedupe_candidates",
            status="success",
            output_data={
                "deduped_count": len(chunks),
                "top_chunks": self._summarize_chunks(chunks, limit=5),
            },
        )
        if not chunks:
            return []

        # Property-scoped filtering: when a booking is active for a specific property,
        # prefer chunks whose source file or content mentions that property.
        if property_filter:
            _pf_lower = str(property_filter).strip().lower()
            _pf_tokens = [t for t in _pf_lower.replace("-", " ").replace("_", " ").split() if len(t) > 2]
            if _pf_tokens:
                def _property_match(chunk: RetrievedChunk) -> bool:
                    haystack = (str(chunk.source or "") + " " + str(chunk.content or "")).lower()
                    return any(token in haystack for token in _pf_tokens)
                matched = [c for c in chunks if _property_match(c)]
                if matched:
                    chunks = matched
                    self._trace_step(
                        trace,
                        step="property_filter",
                        status="success",
                        input_data={"property_filter": property_filter, "tokens": _pf_tokens},
                        output_data={"filtered_count": len(chunks)},
                    )
                else:
                    self._trace_step(
                        trace,
                        step="property_filter",
                        status="no_match",
                        input_data={"property_filter": property_filter},
                        output_data={"reason": "no chunks matched filter, using all chunks"},
                    )

        if self.enable_mmr:
            chunks = self._select_chunks_mmr(
                question=question,
                chunks=chunks,
                top_k=limit,
                lambda_value=self.mmr_lambda,
            )
            self._trace_step(
                trace,
                step="mmr_selection",
                status="success",
                input_data={"mmr_lambda": self.mmr_lambda, "target_top_k": limit},
                output_data={
                    "selected_count": len(chunks),
                    "selected_chunks": self._summarize_chunks(chunks, limit=limit),
                },
            )
        else:
            chunks = chunks[:limit]
            self._trace_step(
                trace,
                step="mmr_selection",
                status="skipped",
                output_data={"reason": "mmr_disabled", "selected_count": len(chunks)},
            )

        if self.enable_rerank:
            chunks = self._rerank(question, chunks, top_k=limit)
            self._trace_step(
                trace,
                step="lexical_rerank",
                status="success",
                output_data={"reranked_chunks": self._summarize_chunks(chunks, limit=limit)},
            )
        else:
            self._trace_step(
                trace,
                step="lexical_rerank",
                status="skipped",
                output_data={"reason": "rerank_disabled"},
            )

        chunks = await self._llm_rerank(question, chunks, trace=trace)
        final_chunks = self._dedupe_chunks(chunks, limit=limit)
        self._trace_step(
            trace,
            step="retrieve_complete",
            status="success",
            output_data={"final_count": len(final_chunks), "final_chunks": self._summarize_chunks(final_chunks, limit=limit)},
        )
        return final_chunks

    async def answer_question(
        self,
        question: str,
        hotel_name: str,
        city: str,
        tenant_id: str = "default",
        business_type: str = "generic",
        property_filter: Optional[str] = None,
    ) -> Optional[RAGAnswer]:
        original_question = re.sub(r"\s+", " ", str(question or "").strip())
        trace = self._new_trace(
            question=original_question,
            tenant_id=tenant_id,
            business_type=business_type,
        )
        final_status = "unknown"
        self._trace_step(
            trace,
            step="input_received",
            status="success",
            input_data={
                "question": self._preview_text(original_question),
                "hotel_name": hotel_name,
                "city": city,
            },
        )

        try:
            rewritten_question = await self._rewrite_query_with_llm(
                original_question,
                business_type=business_type,
            )
            if not self.enable_llm_query_rewrite:
                rewrite_status = "skipped"
                rewrite_reason = "rewrite_disabled"
            elif not settings.openai_api_key:
                rewrite_status = "skipped"
                rewrite_reason = "missing_api_key"
            elif rewritten_question != original_question:
                rewrite_status = "success"
                rewrite_reason = "rewritten"
            else:
                rewrite_status = "no_change"
                rewrite_reason = "unchanged_or_fallback"
            self._trace_step(
                trace,
                step="rewrite_query_llm",
                status=rewrite_status,
                input_data={"original_query": self._preview_text(original_question)},
                output_data={
                    "rewritten_query": self._preview_text(rewritten_question),
                    "reason": rewrite_reason,
                },
            )

            normalized_question = self._normalize_query_text(rewritten_question)
            question_for_analysis = normalized_question or rewritten_question or original_question
            self._trace_step(
                trace,
                step="normalize_query",
                status="success",
                output_data={"normalized_query": self._preview_text(normalized_question)},
            )

            query_tokens = self._tokenize(question_for_analysis)
            question_lower = question_for_analysis.lower()
            is_catalog_query = bool(
                query_tokens & _CATALOG_QUERY_TERMS
                or "in room dining" in question_lower
                or "in-room dining" in question_lower
            )
            is_policy_query = any(marker in question_lower for marker in _POLICY_QUERY_MARKERS)
            if is_catalog_query:
                dynamic_top_k = max(self.top_k, 12)
            elif is_policy_query:
                dynamic_top_k = max(self.top_k, 8)
            else:
                dynamic_top_k = self.top_k
            dynamic_max_tokens = 700 if is_catalog_query else 360
            self._trace_step(
                trace,
                step="query_profile",
                status="success",
                output_data={
                    "is_catalog_query": is_catalog_query,
                    "is_policy_query": is_policy_query,
                    "dynamic_top_k": dynamic_top_k,
                    "dynamic_max_tokens": dynamic_max_tokens,
                },
            )

            chunks = await self.retrieve(
                question=question_for_analysis,
                tenant_id=tenant_id,
                top_k=dynamic_top_k,
                trace=trace,
                property_filter=property_filter,
            )
            if not chunks:
                final_status = "no_chunks"
                self._trace_step(
                    trace,
                    step="answer_decision",
                    status="failed",
                    output_data={"reason": "no_chunks_retrieved"},
                )
                return None

            top_score = chunks[0].score
            self._trace_step(
                trace,
                step="score_gate",
                status="success",
                output_data={
                    "top_score": round(float(top_score), 4),
                    "min_retrieval_score": float(self.min_retrieval_score),
                },
            )
            if top_score < self.min_retrieval_score:
                final_status = "low_retrieval_score"
                self._trace_step(
                    trace,
                    step="answer_decision",
                    status="failed",
                    output_data={"reason": "top_score_below_threshold"},
                )
                return None

            context_blob = "\n\n".join(
                f"[Source: {chunk.source} | Chunk: {chunk.chunk_id}]\n{chunk.content[:1200]}"
                for chunk in chunks
            )
            sources = [f"{chunk.source}#{chunk.chunk_id}" if chunk.chunk_id else chunk.source for chunk in chunks]
            self._trace_step(
                trace,
                step="context_build",
                status="success",
                output_data={
                    "sources": sources,
                    "context_chars": len(context_blob),
                },
            )

            if settings.openai_api_key:
                prompt = (
                    f"You are a {business_type} assistant. Answer ONLY from the provided knowledge context. "
                    "If context is insufficient, say you are not sure and offer human assistance.\n\n"
                    f"Business: {hotel_name}\nCity: {city}\nTenant: {self._normalize_tenant(tenant_id)}\n\n"
                    f"Knowledge Context:\n{context_blob}\n\n"
                    f"Question: {original_question or question_for_analysis}\n\n"
                    "If the question is about menu/food/catalog, list available sections first, then key items with prices from context. "
                    "Do not write placeholder notes like 'details incomplete'; only include fields that exist in context. "
                    "Keep answer concise and practical. Do not invent policies, prices, timings, or capabilities."
                )
                answer = await llm_client.chat(
                    messages=[{"role": "system", "content": prompt}],
                    temperature=0.2,
                    max_tokens=dynamic_max_tokens,
                )
                confidence = min(0.95, top_score + 0.25)
                final_status = "success"
                self._trace_step(
                    trace,
                    step="generate_answer_llm",
                    status="success",
                    input_data={
                        "question_for_llm": original_question or question_for_analysis,
                        "context_preview": self._preview_text(context_blob),
                    },
                    output_data={
                        "answer_preview": self._preview_text(answer),
                        "confidence": round(float(confidence), 4),
                    },
                )
                return RAGAnswer(
                    answer=answer.strip(),
                    confidence=confidence,
                    sources=sources,
                    trace_id=str(trace.get("trace_id") or ""),
                )

            excerpt_lines = chunks[0].content.strip().splitlines()
            excerpt = " ".join(line.strip() for line in excerpt_lines[:5] if line.strip())
            if not excerpt:
                final_status = "no_excerpt"
                self._trace_step(
                    trace,
                    step="generate_answer_fallback",
                    status="failed",
                    output_data={"reason": "empty_excerpt"},
                )
                return None

            answer = (
                f"Based on our available information: {excerpt[:500]} "
                "If you want, I can connect you with our team for exact details."
            )
            confidence = min(0.75, top_score + 0.15)
            final_status = "success"
            self._trace_step(
                trace,
                step="generate_answer_fallback",
                status="success",
                input_data={
                    "question_for_llm": original_question or question_for_analysis,
                    "context_preview": self._preview_text(chunks[0].content),
                },
                output_data={
                    "answer_preview": self._preview_text(answer),
                    "confidence": round(float(confidence), 4),
                },
            )
            return RAGAnswer(
                answer=answer,
                confidence=confidence,
                sources=sources,
                trace_id=str(trace.get("trace_id") or ""),
            )
        finally:
            self._write_trace(trace, final_status=final_status)

    def get_status(self, tenant_id: str = "default") -> dict[str, Any]:
        rows = self._load_local_index()
        normalized_tenant = self._normalize_tenant(tenant_id)
        tenant_rows = [row for row in rows if row.get("tenant_id") == normalized_tenant]
        return {
            "backend_configured": self.backend,
            "qdrant_ready": self._is_qdrant_ready(),
            "local_index_file": str(self.local_index_file),
            "local_total_chunks": len(rows),
            "tenant_id": normalized_tenant,
            "tenant_chunks": len(tenant_rows),
            "kb_dir": str(self.kb_dir),
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "top_k": self.top_k,
            "candidate_pool_min": self.candidate_pool_min,
            "candidate_pool_max": self.candidate_pool_max,
            "mmr_enabled": self.enable_mmr,
            "mmr_lambda": self.mmr_lambda,
            "llm_query_rewrite_enabled": self.enable_llm_query_rewrite,
            "rerank_enabled": self.enable_rerank,
            "llm_rerank_enabled": self.enable_llm_rerank,
            "step_logs_enabled": self.step_logs_enabled,
            "step_log_file": str(self.step_log_file),
        }


# Global singleton
rag_service = RAGService()
