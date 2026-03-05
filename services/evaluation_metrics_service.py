"""
Evaluation Metrics Service

Collects runtime quality and routing telemetry for dashboard/API use.
"""

from __future__ import annotations

from collections import Counter, deque
from datetime import UTC, datetime, timedelta
from typing import Any

from schemas.chat import ChatRequest, ChatResponse


class EvaluationMetricsService:
    """In-memory event tracker for routing/retrieval/policy evaluation."""

    def __init__(self) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=20_000)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _normalize_channel(channel: str | None) -> str:
        value = str(channel or "").strip().lower()
        if value in {"web", "web_widget", "widget", "chat_widget"}:
            return "web"
        if value in {"wa", "whatsapp", "whats_app"}:
            return "whatsapp"
        return value or "web"

    def record_chat_response(
        self,
        request: ChatRequest,
        response: ChatResponse,
        trace_id: str = "",
    ) -> None:
        metadata = response.metadata if isinstance(response.metadata, dict) else {}
        event = {
            "timestamp": self._now(),
            "trace_id": str(trace_id or metadata.get("trace_id") or ""),
            "session_id": str(request.session_id or ""),
            "hotel_code": str(request.hotel_code or ""),
            "channel": self._normalize_channel(
                request.channel or (request.metadata or {}).get("channel")
            ),
            "intent": str(getattr(response.intent, "value", response.intent) or ""),
            "confidence": float(response.confidence or 0.0),
            "state": str(getattr(response.state, "value", response.state) or ""),
            "routing_path": str(metadata.get("routing_path") or "unknown"),
            "response_source": str(metadata.get("response_source") or "unknown"),
            "response_valid": bool(
                metadata.get("response_valid", metadata.get("response_validator_valid", True))
            ),
            "validator_replaced": bool(
                metadata.get("validator_replaced", metadata.get("response_validator_replaced", False))
            ),
            "kb_direct_lookup_used": bool(metadata.get("kb_direct_lookup_used", False)),
            "rag_used": bool(metadata.get("rag_used", False)),
            "agent_orchestration": bool("agent_orchestration" in metadata),
        }
        self._events.append(event)

    def _filtered(self, hours: int) -> list[dict[str, Any]]:
        window_hours = max(1, int(hours))
        cutoff = self._now() - timedelta(hours=window_hours)
        return [event for event in self._events if event["timestamp"] >= cutoff]

    def get_summary(self, hours: int = 24) -> dict[str, Any]:
        events = self._filtered(hours)
        total = len(events)

        if total == 0:
            return {
                "window_hours": max(1, int(hours)),
                "total_messages": 0,
                "routing_breakdown": {},
                "intent_breakdown": {},
                "response_source_breakdown": {},
                "quality": {
                    "validator_replace_rate": 0.0,
                    "low_confidence_rate": 0.0,
                    "complex_path_rate": 0.0,
                    "kb_direct_hit_rate": 0.0,
                    "rag_usage_rate": 0.0,
                    "agent_orchestration_rate": 0.0,
                },
                "alerts": [],
            }

        routing_counter = Counter(event["routing_path"] for event in events)
        intent_counter = Counter(event["intent"] for event in events)
        source_counter = Counter(event["response_source"] for event in events)

        validator_replaced = sum(1 for event in events if event["validator_replaced"])
        low_confidence = sum(1 for event in events if event["confidence"] < 0.5)
        complex_path = sum(1 for event in events if event["routing_path"] == "complex")
        kb_hits = sum(1 for event in events if event["kb_direct_lookup_used"])
        rag_usage = sum(1 for event in events if event["rag_used"])
        agent_usage = sum(1 for event in events if event["agent_orchestration"])

        def pct(value: int) -> float:
            return round((float(value) / float(total)) * 100.0, 2)

        quality = {
            "validator_replace_rate": pct(validator_replaced),
            "low_confidence_rate": pct(low_confidence),
            "complex_path_rate": pct(complex_path),
            "kb_direct_hit_rate": pct(kb_hits),
            "rag_usage_rate": pct(rag_usage),
            "agent_orchestration_rate": pct(agent_usage),
        }

        alerts: list[dict[str, str | float]] = []
        if total >= 20 and quality["validator_replace_rate"] >= 15.0:
            alerts.append(
                {
                    "code": "validator_replace_spike",
                    "severity": "warning",
                    "value": quality["validator_replace_rate"],
                    "message": "Validator replacement rate is elevated.",
                }
            )
        if total >= 20 and quality["low_confidence_rate"] >= 30.0:
            alerts.append(
                {
                    "code": "low_confidence_spike",
                    "severity": "warning",
                    "value": quality["low_confidence_rate"],
                    "message": "Low-confidence classification rate is elevated.",
                }
            )
        if total >= 20 and quality["complex_path_rate"] >= 35.0:
            alerts.append(
                {
                    "code": "complex_path_spike",
                    "severity": "info",
                    "value": quality["complex_path_rate"],
                    "message": "Complex-path routing is above typical baseline.",
                }
            )

        return {
            "window_hours": max(1, int(hours)),
            "total_messages": total,
            "routing_breakdown": dict(routing_counter),
            "intent_breakdown": dict(intent_counter),
            "response_source_breakdown": dict(source_counter),
            "quality": quality,
            "alerts": alerts,
        }

    def get_recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        capped = max(1, min(int(limit), 500))
        events = list(self._events)[-capped:]
        serialized: list[dict[str, Any]] = []
        for event in events:
            row = dict(event)
            timestamp = row.get("timestamp")
            if isinstance(timestamp, datetime):
                row["timestamp"] = timestamp.isoformat()
            serialized.append(row)
        return serialized


evaluation_metrics_service = EvaluationMetricsService()
