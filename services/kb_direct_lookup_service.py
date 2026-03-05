"""
KB Direct Lookup Service

Deterministic knowledge lookup over uploaded KB files.
This is intentionally separate from vector RAG and can be run before RAG
as a high-precision path for structured property facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import json
import re
import uuid
from typing import Any, Optional

from config.settings import settings
from llm.client import llm_client
from services.config_service import config_service


DEFAULT_KB_DIR = Path(__file__).parent.parent / "config" / "knowledge_base"
_KB_EXTENSIONS = (".txt", ".json", ".md", ".markdown", ".rst")


@dataclass
class KBFact:
    key: str
    display_key: str
    value: str
    source: str
    key_tokens: set[str]
    value_tokens: set[str]


@dataclass
class KBDirectLookupResult:
    handled: bool
    answer: str = ""
    confidence: float = 0.0
    reason: str = ""
    matched_field: str = ""
    source_file: str = ""
    trace_id: str = ""


class KBDirectLookupService:
    """Deterministic KB lookup with light query normalization and rule routing."""

    def __init__(self, kb_dir: Optional[Path] = None):
        self.kb_dir = Path(kb_dir or DEFAULT_KB_DIR)
        self.min_score = max(0.0, min(1.0, float(getattr(settings, "kb_direct_lookup_min_score", 0.34))))
        self.max_answer_chars = max(220, int(getattr(settings, "kb_direct_lookup_max_answer_chars", 600)))
        self.step_logs_enabled = bool(getattr(settings, "kb_direct_lookup_step_logs_enabled", True))
        self.step_log_file = Path(getattr(settings, "rag_step_log_file", "./logs/detailedsteps.log"))
        self.enable_llm_rewrite = bool(getattr(settings, "kb_direct_lookup_enable_llm_rewrite", True))
        self.llm_rewrite_max_tokens = max(16, int(getattr(settings, "kb_direct_lookup_llm_rewrite_max_tokens", 64)))

        self._cache_signature = ""
        self._cache_facts: list[KBFact] = []
        self._cache_by_key: dict[str, KBFact] = {}

    @staticmethod
    def _normalize_tenant(value: str) -> str:
        return str(value or "default").strip().lower().replace(" ", "_")

    @staticmethod
    def _normalize_key(raw_key: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "_", str(raw_key or "").strip().lower())
        return normalized.strip("_")

    @staticmethod
    def _normalize_query(query: str) -> str:
        value = str(query or "").strip().lower()
        value = re.sub(r"\|\s*context\s*:.*$", "", value, flags=re.IGNORECASE).strip()
        value = value.replace("wi-fi", "wifi").replace("wi fi", "wifi")
        value = value.replace("t1", "terminal 1").replace("t2", "terminal 2")
        value = re.sub(r"\badress\b", "address", value)
        value = re.sub(r"\bavaalble\b", "available", value)
        value = re.sub(r"\bavaiable\b", "available", value)
        value = re.sub(r"\bameticies\b", "amenities", value)
        value = re.sub(r"\bflovors\b", "flavors", value)
        value = re.sub(r"\s+", " ", value)
        return value

    @staticmethod
    def _is_reasonable_query_rewrite(original: str, rewritten: str) -> bool:
        base = str(original or "").strip().lower()
        candidate = str(rewritten or "").strip().lower()
        if not base or not candidate:
            return False
        if candidate == base:
            return True
        if len(candidate) > 260:
            return False

        base_tokens = KBDirectLookupService._tokenize(base)
        candidate_tokens = KBDirectLookupService._tokenize(candidate)
        if not base_tokens or not candidate_tokens:
            return False

        overlap = len(base_tokens & candidate_tokens) / max(1, len(base_tokens))
        return overlap >= 0.4

    async def _rewrite_query_with_llm(self, query: str) -> str:
        original = re.sub(r"\s+", " ", str(query or "").strip())
        if not original:
            return ""
        if not self.enable_llm_rewrite or not settings.openai_api_key:
            return original

        prompt = (
            "You normalize user queries for deterministic KB lookup.\n"
            "Correct typos/spelling and minor grammar only.\n"
            "Do not add or remove intent, entities, dates, times, or constraints.\n"
            "Return exactly one rewritten query line and nothing else."
        )
        try:
            rewritten = await llm_client.chat(
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": original},
                ],
                temperature=0.0,
                max_tokens=self.llm_rewrite_max_tokens,
            )
        except Exception:
            return original

        candidate = re.sub(r"\s+", " ", str(rewritten or "").strip().strip('"').strip("'"))
        if not candidate:
            return original
        if self._is_reasonable_query_rewrite(original, candidate):
            return candidate
        return original

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return set(re.findall(r"[a-z0-9]+", str(text or "").lower()))

    @staticmethod
    def _expand_tokens(tokens: set[str]) -> set[str]:
        expanded = set(tokens)
        for token in list(tokens):
            if token in {"wifi", "internet", "wireless"}:
                expanded.update({"wifi", "internet", "wireless", "wi", "fi"})
            if token in {"bar", "alcohol", "cocktail", "wine", "drink", "drinks"}:
                expanded.update({"bar", "alcohol", "cocktail", "wine", "drink", "drinks", "liquor", "beer"})
            if token in {"address", "location"}:
                expanded.update({"address", "location", "where"})
            if token in {"terminal", "airport"}:
                expanded.update({"terminal", "airport", "t1", "t2", "near"})
            if token in {"smart", "laundry", "closet"}:
                expanded.update({"smart", "laundry", "closet", "steam", "iron", "refresh"})
            if token in {"largest", "biggest", "largest"}:
                expanded.update({"largest", "biggest", "spacious", "room", "suite"})
        return expanded

    @staticmethod
    def _render_value(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, indent=2)
        return str(value)

    @staticmethod
    def _coalesce_space(value: str) -> str:
        text = str(value or "").replace("\r\n", "\n")
        text = text.replace("\\r\\n", "\n").replace("\\n", "\n")
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    def _new_trace(self, question: str, tenant_id: str) -> Optional[dict[str, Any]]:
        if not self.step_logs_enabled:
            return None
        return {
            "trace_id": f"kb-{uuid.uuid4().hex[:12]}",
            "started_at": datetime.now(UTC).isoformat(),
            "tenant_id": self._normalize_tenant(tenant_id),
            "question": str(question or "").strip(),
            "steps": [],
        }

    def _trace_step(
        self,
        trace: Optional[dict[str, Any]],
        step: str,
        status: str,
        input_data: Optional[dict[str, Any]] = None,
        output_data: Optional[dict[str, Any]] = None,
        error: str = "",
    ) -> None:
        if trace is None:
            return
        payload: dict[str, Any] = {
            "time": datetime.now(UTC).isoformat(),
            "step": step,
            "status": status,
        }
        if input_data:
            payload["input"] = input_data
        if output_data:
            payload["output"] = output_data
        if error:
            payload["error"] = str(error)
        trace["steps"].append(payload)

    def _render_human_trace(self, trace: dict[str, Any]) -> str:
        question = str(trace.get("question") or "")
        rewrite_status = "unknown"
        rewritten_query = ""
        normalized_query = ""
        matched_field = ""
        answer_text = ""
        status = "unknown"
        decision_reason = ""

        for step in trace.get("steps", []):
            if not isinstance(step, dict):
                continue
            if step.get("step") == "rewrite_query_llm":
                rewrite_status = str(step.get("status") or rewrite_status)
                rewritten_query = str((step.get("output") or {}).get("rewritten_query") or "")
            if step.get("step") == "normalize_query":
                normalized_query = str((step.get("output") or {}).get("normalized_query") or "")
            if step.get("step") == "match_decision":
                status = str(step.get("status") or status)
                matched_field = str((step.get("output") or {}).get("matched_field") or "")
                answer_text = str((step.get("output") or {}).get("answer_preview") or "")
                decision_reason = str((step.get("output") or {}).get("reason") or "")

        lines = [
            f"===== KB DIRECT LOOKUP TRACE {trace.get('trace_id', '')} =====",
            f"Tenant: {trace.get('tenant_id', '')} | Status: {status}",
            f"Started: {trace.get('started_at', '')} | Completed: {trace.get('completed_at', '')}",
            "",
            "1) USER QUERY",
            f"   {question}",
            "",
            f"2) LLM REWRITE ({rewrite_status})",
            f"   {rewritten_query or question}",
            "",
            "3) NORMALIZED LOOKUP QUERY",
            f"   {normalized_query or question}",
            "",
            "4) MATCHED FIELD",
            f"   {matched_field or 'None'}",
            "",
            "5) OUTPUT",
            f"   {answer_text}",
            "",
            "6) DECISION REASON",
            f"   {decision_reason or '-'}",
            "===== END KB DIRECT LOOKUP TRACE =====",
            "",
        ]
        return "\n".join(lines)

    def _write_trace(self, trace: Optional[dict[str, Any]], final_status: str) -> None:
        if trace is None:
            return
        trace["completed_at"] = datetime.now(UTC).isoformat()
        trace["final_status"] = str(final_status or "unknown")
        try:
            self.step_log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.step_log_file.open("a", encoding="utf-8") as fh:
                fh.write(self._render_human_trace(trace))
                fh.write(f"--- KB DIRECT TRACE {trace.get('trace_id', '')} ---\n")
                fh.write(json.dumps(trace, ensure_ascii=False, indent=2))
                fh.write("\n--- END KB DIRECT TRACE ---\n")
        except Exception:
            return

    def _resolve_source_paths(self, tenant_id: str, source_paths: Optional[list[str | Path]]) -> list[Path]:
        if source_paths:
            manual_paths: list[Path] = []
            for source in source_paths:
                path = Path(str(source))
                if path.exists() and path.is_file():
                    manual_paths.append(path.resolve())
            return manual_paths

        resolved: list[Path] = []
        knowledge_sources = config_service.get_knowledge_config().get("sources", [])
        if isinstance(knowledge_sources, list):
            for source in knowledge_sources:
                if not isinstance(source, str):
                    continue
                path = Path(source)
                if path.exists() and path.is_file():
                    resolved.append(path.resolve())

        normalized_tenant = self._normalize_tenant(tenant_id)
        tenant_uploads = self.kb_dir / "uploads" / normalized_tenant
        if tenant_uploads.exists():
            for candidate in tenant_uploads.iterdir():
                if candidate.is_file() and candidate.suffix.lower() in _KB_EXTENSIONS:
                    resolved.append(candidate.resolve())

        default_uploads = self.kb_dir / "uploads" / "default"
        if default_uploads.exists():
            for candidate in default_uploads.iterdir():
                if candidate.is_file() and candidate.suffix.lower() in _KB_EXTENSIONS:
                    resolved.append(candidate.resolve())

        if self.kb_dir.exists():
            for candidate in self.kb_dir.iterdir():
                if candidate.is_file() and candidate.suffix.lower() in _KB_EXTENSIONS:
                    resolved.append(candidate.resolve())

        deduped: list[Path] = []
        seen: set[str] = set()
        for path in sorted(
            resolved,
            key=lambda p: p.stat().st_mtime_ns if p.exists() else 0,
            reverse=True,
        ):
            marker = str(path)
            if marker in seen:
                continue
            seen.add(marker)
            deduped.append(path)
        return deduped

    @staticmethod
    def _extract_editable(payload: Any) -> Optional[dict[str, Any]]:
        if not isinstance(payload, dict):
            return None

        if isinstance(payload.get("editable"), dict):
            return payload.get("editable")

        inner_data = payload.get("data")
        if isinstance(inner_data, str):
            try:
                inner = json.loads(inner_data)
            except Exception:
                inner = None
            if isinstance(inner, dict):
                if isinstance(inner.get("editable"), dict):
                    return inner.get("editable")
                return inner

        reserved = {"data", "orgId", "org_id", "tenant_id", "business_type"}
        editable_like = {k: v for k, v in payload.items() if k not in reserved}
        if editable_like:
            return editable_like
        return None

    def _load_facts(self, paths: list[Path]) -> tuple[list[KBFact], dict[str, KBFact]]:
        signature_parts: list[str] = []
        for path in paths:
            try:
                signature_parts.append(f"{path}:{path.stat().st_mtime_ns}")
            except Exception:
                continue
        signature = "|".join(signature_parts)
        if signature and signature == self._cache_signature:
            return self._cache_facts, self._cache_by_key

        facts: list[KBFact] = []
        by_key: dict[str, KBFact] = {}
        seen_records: set[tuple[str, str]] = set()

        for path in paths:
            try:
                raw = path.read_text(encoding="utf-8", errors="ignore").strip()
            except Exception:
                continue
            if not raw:
                continue

            payload: Any = None
            try:
                payload = json.loads(raw)
            except Exception:
                payload = None
            editable = self._extract_editable(payload)
            if not isinstance(editable, dict):
                continue

            for key, value in editable.items():
                normalized_key = self._normalize_key(str(key))
                rendered = self._coalesce_space(self._render_value(value))
                if not normalized_key or not rendered:
                    continue
                record_signature = (normalized_key, rendered[:2200])
                if record_signature in seen_records:
                    continue
                seen_records.add(record_signature)

                key_tokens = self._expand_tokens(self._tokenize(normalized_key.replace("_", " ")))
                value_tokens = self._expand_tokens(self._tokenize(rendered[:4000]))
                fact = KBFact(
                    key=normalized_key,
                    display_key=str(key),
                    value=rendered,
                    source=str(path),
                    key_tokens=key_tokens,
                    value_tokens=value_tokens,
                )
                facts.append(fact)
                if normalized_key not in by_key:
                    by_key[normalized_key] = fact

        self._cache_signature = signature
        self._cache_facts = facts
        self._cache_by_key = by_key
        return facts, by_key

    @staticmethod
    def _first_line_containing(value: str, terms: tuple[str, ...]) -> str:
        for line in value.splitlines():
            line_clean = re.sub(r"\s+", " ", line).strip()
            if not line_clean:
                continue
            lower = line_clean.lower()
            if any(term in lower for term in terms):
                return line_clean
        return ""

    @staticmethod
    def _extract_sqft_max(value: str) -> int:
        text = str(value or "").lower()
        max_area = 0

        for match in re.finditer(r"(\d{2,4})\s*[-–to]{1,3}\s*(\d{2,4})\s*sq\.?\s*ft", text):
            left = int(match.group(1))
            right = int(match.group(2))
            max_area = max(max_area, left, right)

        for match in re.finditer(r"(\d{2,4})\s*sq\.?\s*ft", text):
            max_area = max(max_area, int(match.group(1)))

        return max_area

    @staticmethod
    def _human_key(key: str) -> str:
        return re.sub(r"\s+", " ", key.replace("_", " ")).strip().title()

    def _best_fact_match(self, query: str, facts: list[KBFact]) -> tuple[Optional[KBFact], float]:
        query_tokens = self._expand_tokens(self._tokenize(query))
        if not query_tokens:
            return None, 0.0

        best_fact: Optional[KBFact] = None
        best_score = 0.0
        for fact in facts:
            key_overlap = len(query_tokens & fact.key_tokens) / max(1, len(query_tokens))
            value_overlap = len(query_tokens & fact.value_tokens) / max(1, len(query_tokens))
            score = (0.7 * key_overlap) + (0.3 * value_overlap)

            if query and query in fact.value.lower():
                score += 0.18
            if query and query in fact.key.replace("_", " "):
                score += 0.2

            if score > best_score:
                best_score = score
                best_fact = fact
        return best_fact, best_score

    def _compose_generic_answer(self, query: str, fact: KBFact) -> str:
        query_tokens = self._expand_tokens(self._tokenize(query))
        lines = [re.sub(r"\s+", " ", line).strip() for line in fact.value.splitlines() if line.strip()]
        if not lines:
            return fact.value[: self.max_answer_chars]

        selected: list[str] = []
        for line in lines:
            line_tokens = self._expand_tokens(self._tokenize(line))
            if query_tokens & line_tokens:
                selected.append(line)
            if len(selected) >= 3:
                break
        if not selected:
            selected = lines[:2]

        answer = " ".join(selected).strip()
        answer = re.sub(r"\s+", " ", answer)
        return answer[: self.max_answer_chars]

    def _heuristic_answer(self, query: str, by_key: dict[str, KBFact], facts: list[KBFact]) -> Optional[KBDirectLookupResult]:
        q = query
        q_tokens = self._tokenize(q)

        def get_fact(*keys: str) -> Optional[KBFact]:
            for key in keys:
                fact = by_key.get(key)
                if fact is not None:
                    return fact
            return None

        # Floors
        if "floor" in q_tokens:
            fact = get_fact("total_floors")
            if fact:
                return KBDirectLookupResult(
                    handled=True,
                    answer=f"The hotel has {fact.value}.",
                    confidence=0.96,
                    reason="heuristic_total_floors",
                    matched_field=fact.display_key,
                    source_file=fact.source,
                )

        # Address
        if {"address", "location", "where"} & q_tokens:
            fact = get_fact("hotel_address", "address")
            if fact:
                return KBDirectLookupResult(
                    handled=True,
                    answer=f"The hotel address is {fact.value}.",
                    confidence=0.96,
                    reason="heuristic_address",
                    matched_field=fact.display_key,
                    source_file=fact.source,
                )

        # Terminal proximity / T1 vs T2
        if "terminal" in q_tokens or {"airport", "t1", "t2", "closer", "near"} & q_tokens:
            airport_fact = get_fact("airport_transfer")
            address_fact = get_fact("hotel_address", "address")
            sales_fact = get_fact("hotel_sales_script")
            airport_text = (airport_fact.value.lower() if airport_fact else "")
            address_text = (address_fact.value.lower() if address_fact else "")
            sales_text = (sales_fact.value.lower() if sales_fact else "")
            mentions_t2_near = "near terminal 2" in address_text or "300 meters from terminal 2" in sales_text

            if "closer" in q_tokens and "terminal" in q_tokens and "1" in q_tokens and "2" in q_tokens:
                if mentions_t2_near or "for t2" in airport_text:
                    return KBDirectLookupResult(
                        handled=True,
                        answer="Terminal 2 is closer. The property is near Terminal 2 and transfer pricing is lower for T2.",
                        confidence=0.91,
                        reason="heuristic_terminal_comparison",
                        matched_field=(airport_fact.display_key if airport_fact else (address_fact.display_key if address_fact else "")),
                        source_file=(airport_fact.source if airport_fact else (address_fact.source if address_fact else "")),
                    )

            if "terminal" in q_tokens and "2" in q_tokens:
                if mentions_t2_near:
                    return KBDirectLookupResult(
                        handled=True,
                        answer="Yes, the hotel is near Terminal 2 (around 300 meters, per the property notes).",
                        confidence=0.93,
                        reason="heuristic_terminal2_near",
                        matched_field=(address_fact.display_key if address_fact else (sales_fact.display_key if sales_fact else "")),
                        source_file=(address_fact.source if address_fact else (sales_fact.source if sales_fact else "")),
                    )

        # Largest room
        if {"largest", "biggest", "spacious"} & q_tokens and {"room", "suite"} & q_tokens:
            candidate_facts = [
                fact
                for key, fact in by_key.items()
                if (
                    ("suite" in key or key.endswith("_room"))
                    and key not in {"room_distribution", "total_rooms"}
                )
            ]
            best_fact: Optional[KBFact] = None
            best_area = 0
            for fact in candidate_facts:
                area = self._extract_sqft_max(fact.value)
                if area > best_area:
                    best_area = area
                    best_fact = fact
            if best_fact and best_area > 0:
                return KBDirectLookupResult(
                    handled=True,
                    answer=f"The largest guest room category is {self._human_key(best_fact.key)} at about {best_area} sq. ft.",
                    confidence=0.9,
                    reason="heuristic_largest_room",
                    matched_field=best_fact.display_key,
                    source_file=best_fact.source,
                )

        # Prestige bathtub
        if "prestige" in q_tokens and ("bathtub" in q_tokens or "bath" in q_tokens):
            fact = get_fact("prestige_suite")
            if fact and "bathtub" in fact.value.lower():
                return KBDirectLookupResult(
                    handled=True,
                    answer="Yes, the Prestige Suite includes a bathtub.",
                    confidence=0.97,
                    reason="heuristic_prestige_bathtub",
                    matched_field=fact.display_key,
                    source_file=fact.source,
                )

        # WiFi
        if {"wifi", "internet", "wireless"} & q_tokens:
            fact = get_fact("in_room_amenities")
            if fact and "wifi" in fact.value.lower():
                wifi_line = self._first_line_containing(fact.value, ("wifi",))
                if not wifi_line:
                    wifi_line = "High-speed WiFi with unlimited device usage"
                return KBDirectLookupResult(
                    handled=True,
                    answer=f"Yes. {wifi_line}.",
                    confidence=0.96,
                    reason="heuristic_wifi",
                    matched_field=fact.display_key,
                    source_file=fact.source,
                )
            for fact_candidate in facts:
                if "wifi" in fact_candidate.value.lower():
                    wifi_line = self._first_line_containing(fact_candidate.value, ("wifi",))
                    return KBDirectLookupResult(
                        handled=True,
                        answer=f"Yes. {wifi_line or 'High-speed WiFi is available in rooms.'}",
                        confidence=0.88,
                        reason="heuristic_wifi_fallback",
                        matched_field=fact_candidate.display_key,
                        source_file=fact_candidate.source,
                    )

        # Smart laundry closet
        if "smart laundry closet" in q or ({"smart", "laundry", "closet"} <= q_tokens):
            for fact in facts:
                if "smart laundry closet" in fact.value.lower():
                    line = self._first_line_containing(fact.value, ("smart laundry closet",))
                    return KBDirectLookupResult(
                        handled=True,
                        answer=(
                            "It is an in-room feature to iron, steam, and refresh clothes without waiting for service."
                            if not line
                            else line
                        ),
                        confidence=0.93,
                        reason="heuristic_smart_laundry",
                        matched_field=fact.display_key,
                        source_file=fact.source,
                    )

        # Alcohol and bar
        if {"bar", "alcohol", "wine", "cocktail", "drink", "drinks"} & q_tokens:
            restaurant_fact = get_fact("restaurant_info")
            amenity_fact = get_fact("in_room_amenities")
            restaurant_text = restaurant_fact.value.lower() if restaurant_fact else ""
            has_bar = "bar" in restaurant_text
            if has_bar:
                answer = "Yes, bar service is available (including Aviation Bar and other bar areas listed by the property)."
                if "alcohol" in q_tokens and amenity_fact and "non-alcoholic minibar" in amenity_fact.value.lower():
                    answer += " Note: the in-room minibar is non-alcoholic."
                return KBDirectLookupResult(
                    handled=True,
                    answer=answer,
                    confidence=0.9,
                    reason="heuristic_bar_alcohol",
                    matched_field=(restaurant_fact.display_key if restaurant_fact else ""),
                    source_file=(restaurant_fact.source if restaurant_fact else ""),
                )

        return None

    def _answer_question_with_normalized_query(
        self,
        normalized_query: str,
        tenant_id: str,
        source_paths: Optional[list[str | Path]],
        trace: Optional[dict[str, Any]],
        trace_id: str,
    ) -> tuple[KBDirectLookupResult, str]:
        if not normalized_query:
            self._trace_step(
                trace,
                step="match_decision",
                status="failed",
                output_data={"reason": "empty_query"},
            )
            return KBDirectLookupResult(handled=False, reason="empty_query", trace_id=trace_id), "empty_query"

        paths = self._resolve_source_paths(tenant_id=tenant_id, source_paths=source_paths)
        self._trace_step(
            trace,
            step="resolve_sources",
            status="success",
            output_data={
                "sources_count": len(paths),
                "sources_preview": [path.name for path in paths[:6]],
            },
        )

        if not paths:
            self._trace_step(
                trace,
                step="match_decision",
                status="failed",
                output_data={"reason": "no_kb_sources"},
            )
            return KBDirectLookupResult(handled=False, reason="no_kb_sources", trace_id=trace_id), "no_sources"

        facts, by_key = self._load_facts(paths)
        self._trace_step(
            trace,
            step="load_facts",
            status="success",
            output_data={"facts_count": len(facts), "keys_count": len(by_key)},
        )
        if not facts:
            self._trace_step(
                trace,
                step="match_decision",
                status="failed",
                output_data={"reason": "no_structured_facts"},
            )
            return (
                KBDirectLookupResult(
                    handled=False,
                    reason="no_structured_facts",
                    trace_id=trace_id,
                ),
                "no_facts",
            )

        heuristic = self._heuristic_answer(normalized_query, by_key, facts)
        if heuristic and heuristic.handled:
            heuristic.trace_id = trace_id
            self._trace_step(
                trace,
                step="match_decision",
                status="success",
                output_data={
                    "strategy": "heuristic",
                    "matched_field": heuristic.matched_field,
                    "reason": heuristic.reason,
                    "answer_preview": heuristic.answer[:220],
                    "confidence": heuristic.confidence,
                },
            )
            return heuristic, "success"

        best_fact, score = self._best_fact_match(normalized_query, facts)
        if best_fact is None or score < self.min_score:
            self._trace_step(
                trace,
                step="match_decision",
                status="failed",
                output_data={
                    "reason": "score_below_threshold",
                    "best_score": round(float(score), 4),
                    "threshold": float(self.min_score),
                },
            )
            return (
                KBDirectLookupResult(
                    handled=False,
                    reason="score_below_threshold",
                    trace_id=trace_id,
                ),
                "no_match",
            )

        answer = self._compose_generic_answer(normalized_query, best_fact)
        result = KBDirectLookupResult(
            handled=True,
            answer=answer,
            confidence=min(0.92, max(0.65, float(score) + 0.08)),
            reason="generic_fact_match",
            matched_field=best_fact.display_key,
            source_file=best_fact.source,
            trace_id=trace_id,
        )
        self._trace_step(
            trace,
            step="match_decision",
            status="success",
            output_data={
                "strategy": "generic_fact_match",
                "matched_field": result.matched_field,
                "score": round(float(score), 4),
                "answer_preview": result.answer[:220],
            },
        )
        return result, "success"

    def answer_question(
        self,
        query: str,
        tenant_id: str = "default",
        source_paths: Optional[list[str | Path]] = None,
    ) -> KBDirectLookupResult:
        original_query = str(query or "").strip()
        trace = self._new_trace(original_query, tenant_id)
        trace_id = str((trace or {}).get("trace_id") or "")
        final_status = "unknown"

        self._trace_step(
            trace,
            step="input_received",
            status="success",
            input_data={"query": original_query},
        )
        try:
            normalized_query = self._normalize_query(original_query)
            self._trace_step(
                trace,
                step="normalize_query",
                status="success",
                output_data={"normalized_query": normalized_query},
            )
            result, final_status = self._answer_question_with_normalized_query(
                normalized_query=normalized_query,
                tenant_id=tenant_id,
                source_paths=source_paths,
                trace=trace,
                trace_id=trace_id,
            )
            return result
        finally:
            self._write_trace(trace, final_status=final_status)

    async def answer_question_async(
        self,
        query: str,
        tenant_id: str = "default",
        source_paths: Optional[list[str | Path]] = None,
    ) -> KBDirectLookupResult:
        original_query = str(query or "").strip()
        trace = self._new_trace(original_query, tenant_id)
        trace_id = str((trace or {}).get("trace_id") or "")
        final_status = "unknown"

        self._trace_step(
            trace,
            step="input_received",
            status="success",
            input_data={"query": original_query},
        )
        try:
            rewritten_query = await self._rewrite_query_with_llm(original_query)
            if not self.enable_llm_rewrite:
                rewrite_status = "skipped"
                rewrite_reason = "rewrite_disabled"
            elif not settings.openai_api_key:
                rewrite_status = "skipped"
                rewrite_reason = "missing_api_key"
            elif rewritten_query != original_query:
                rewrite_status = "success"
                rewrite_reason = "rewritten"
            else:
                rewrite_status = "no_change"
                rewrite_reason = "unchanged_or_fallback"
            self._trace_step(
                trace,
                step="rewrite_query_llm",
                status=rewrite_status,
                input_data={"original_query": original_query},
                output_data={
                    "rewritten_query": rewritten_query,
                    "reason": rewrite_reason,
                },
            )

            normalized_query = self._normalize_query(rewritten_query or original_query)
            self._trace_step(
                trace,
                step="normalize_query",
                status="success",
                output_data={"normalized_query": normalized_query},
            )
            result, final_status = self._answer_question_with_normalized_query(
                normalized_query=normalized_query,
                tenant_id=tenant_id,
                source_paths=source_paths,
                trace=trace,
                trace_id=trace_id,
            )
            return result
        finally:
            self._write_trace(trace, final_status=final_status)


kb_direct_lookup_service = KBDirectLookupService()
