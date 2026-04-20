"""
Simple per-call LLM usage logger.

Writes one JSON record per LLM call with:
- time taken
- input/output/total tokens
- cost
- plain-English purpose
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

from config.settings import settings


def _safe_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _safe_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    if parsed != parsed:
        return 0.0
    return max(0.0, parsed)


def _truncate_text(value: Any, max_chars: int) -> str:
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return f"{text[:max_chars]}\n...[TRUNCATED {omitted} chars]"


class LLMSimpleCallLogger:
    """Append-only JSONL logger for LLM call accounting."""

    _DEFAULT_PRICING_USD_PER_1M: dict[str, dict[str, float]] = {
        "gpt-4o": {"input": 5.0, "output": 15.0},
        "gpt-4o-mini": {"input": 0.15, "output": 0.60},
        "text-embedding-3-small": {"input": 0.02, "output": 0.0},
        "text-embedding-3-large": {"input": 0.13, "output": 0.0},
    }

    def __init__(self) -> None:
        self.enabled = bool(getattr(settings, "llm_call_simple_logging_enabled", True))
        self.log_file = Path(str(getattr(settings, "llm_call_simple_log_file", "./logs/llm_calls_simple.jsonl")))
        self.max_text_chars = max(200, int(getattr(settings, "llm_call_simple_text_max_chars", 1200) or 1200))
        self._lock = threading.Lock()
        self._pricing = dict(self._DEFAULT_PRICING_USD_PER_1M)
        self._load_pricing_override()

    def _load_pricing_override(self) -> None:
        raw = str(getattr(settings, "llm_model_pricing_json", "") or "").strip()
        if not raw:
            return
        try:
            parsed = json.loads(raw)
        except Exception:
            return
        if not isinstance(parsed, dict):
            return
        normalized: dict[str, dict[str, float]] = {}
        for model_name, rates in parsed.items():
            if not isinstance(rates, dict):
                continue
            name = str(model_name or "").strip()
            if not name:
                continue
            normalized[name] = {
                "input": _safe_float(rates.get("input") or rates.get("input_per_million") or 0.0),
                "output": _safe_float(rates.get("output") or rates.get("output_per_million") or 0.0),
            }
        if normalized:
            self._pricing.update(normalized)

    def _resolve_pricing(self, model: str) -> tuple[float, float]:
        name = str(model or "").strip()
        if not name:
            return 0.0, 0.0
        if name in self._pricing:
            rates = self._pricing[name]
            return _safe_float(rates.get("input")), _safe_float(rates.get("output"))
        # Handle model snapshots such as "gpt-4o-2024-08-06"
        for known_name, rates in self._pricing.items():
            if name.startswith(f"{known_name}-"):
                return _safe_float(rates.get("input")), _safe_float(rates.get("output"))
        return 0.0, 0.0

    def _compute_cost(
        self,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> tuple[float, float, float]:
        input_rate, output_rate = self._resolve_pricing(model)
        input_cost = (_safe_int(input_tokens) / 1_000_000.0) * input_rate
        output_cost = (_safe_int(output_tokens) / 1_000_000.0) * output_rate
        total_cost = input_cost + output_cost
        return input_cost, output_cost, total_cost

    def log_call(
        self,
        *,
        call_id: str,
        operation: str,
        model: str,
        purpose: str,
        duration_ms: float,
        status: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        session_id: str = "",
        trace_id: str = "",
        turn_trace_id: str = "",
        route: str = "",
        component: str = "",
        caller_module: str = "",
        caller_function: str = "",
        input_preview: str = "",
        output_preview: str = "",
        error: str = "",
    ) -> None:
        if not self.enabled:
            return
        input_tok = _safe_int(input_tokens)
        output_tok = _safe_int(output_tokens)
        total_tok = _safe_int(total_tokens) or (input_tok + output_tok)
        input_cost, output_cost, total_cost = self._compute_cost(
            model=model,
            input_tokens=input_tok,
            output_tokens=output_tok,
        )
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "call_id": str(call_id or "").strip(),
            "operation": str(operation or "").strip(),
            "model": str(model or "").strip(),
            "what_this_call_is_for": str(purpose or "").strip() or "LLM call",
            "status": str(status or "").strip() or "unknown",
            "duration_ms": round(_safe_float(duration_ms), 2),
            "input_tokens": input_tok,
            "output_tokens": output_tok,
            "total_tokens": total_tok,
            "input_cost_usd": round(input_cost, 8),
            "output_cost_usd": round(output_cost, 8),
            "total_cost_usd": round(total_cost, 8),
            "session_id": str(session_id or "").strip(),
            "trace_id": str(trace_id or "").strip(),
            "turn_trace_id": str(turn_trace_id or "").strip(),
            "route": str(route or "").strip(),
            "component": str(component or "").strip(),
            "caller_module": str(caller_module or "").strip(),
            "caller_function": str(caller_function or "").strip(),
            "input_preview": _truncate_text(input_preview, self.max_text_chars),
            "output_preview": _truncate_text(output_preview, self.max_text_chars),
            "error": _truncate_text(error, self.max_text_chars) if error else "",
        }
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            encoded = json.dumps(record, ensure_ascii=False)
            with self._lock:
                with self.log_file.open("a", encoding="utf-8") as fh:
                    fh.write(encoded + "\n")
        except Exception:
            return


llm_simple_call_logger = LLMSimpleCallLogger()

