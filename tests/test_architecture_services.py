from config.settings import settings
from schemas.chat import ChatRequest, ChatResponse, ConversationState, IntentType
from services.evaluation_metrics_service import EvaluationMetricsService
from services.gateway_service import GatewayService
from services.observability_service import ObservabilityService


def test_evaluation_metrics_summary_and_alerts():
    service = EvaluationMetricsService()

    for idx in range(25):
        request = ChatRequest(
            session_id=f"s{idx}",
            message="question",
            hotel_code="default",
            channel="web_widget",
        )
        response = ChatResponse(
            session_id=f"s{idx}",
            message="answer",
            intent=IntentType.FAQ,
            confidence=0.4,
            state=ConversationState.IDLE,
            metadata={
                "routing_path": "complex",
                "response_source": "agent_orchestrator",
                "validator_replaced": True,
                "rag_used": True,
                "agent_orchestration": {"handled": True},
            },
        )
        service.record_chat_response(request=request, response=response, trace_id=f"trc-{idx}")

    summary = service.get_summary(hours=24)
    assert summary["total_messages"] == 25
    assert summary["quality"]["validator_replace_rate"] == 100.0
    assert summary["quality"]["complex_path_rate"] == 100.0
    assert summary["quality"]["low_confidence_rate"] == 100.0
    assert summary["quality"]["agent_orchestration_rate"] == 100.0
    alert_codes = {alert["code"] for alert in summary["alerts"]}
    assert "validator_replace_spike" in alert_codes
    assert "low_confidence_spike" in alert_codes
    assert "complex_path_spike" in alert_codes


def test_observability_service_status_and_tail(tmp_path, monkeypatch):
    log_path = tmp_path / "observability.log"
    monkeypatch.setattr(settings, "observability_enabled", True, raising=False)
    monkeypatch.setattr(settings, "observability_log_file", str(log_path), raising=False)

    service = ObservabilityService()
    service.log_event("chat_message_processed", {"trace_id": "trc-123", "status_code": 200})

    status = service.get_status()
    assert status["enabled"] is True
    assert status["exists"] is True
    assert status["size_bytes"] > 0

    rows = service.read_recent_events(limit=10, event_filter="chat_message")
    assert len(rows) == 1
    assert rows[0]["event"] == "chat_message_processed"
    assert rows[0]["payload"]["trace_id"] == "trc-123"


def test_gateway_service_auth_rate_limit_snapshot(monkeypatch):
    monkeypatch.setattr(settings, "api_gateway_auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "api_gateway_api_key", "secret", raising=False)
    monkeypatch.setattr(settings, "api_gateway_rate_limit_enabled", True, raising=False)
    monkeypatch.setattr(settings, "api_gateway_rate_limit_requests", 2, raising=False)
    monkeypatch.setattr(settings, "api_gateway_rate_limit_window_seconds", 60, raising=False)

    service = GatewayService()
    assert service.is_authorized("secret") is True
    assert service.is_authorized("wrong") is False

    ok1, _ = service.allow_request("client-a")
    ok2, _ = service.allow_request("client-a")
    ok3, retry_after = service.allow_request("client-a")
    assert ok1 is True
    assert ok2 is True
    assert ok3 is False
    assert retry_after >= 1

    snapshot = service.snapshot_state()
    assert snapshot["auth_required"] is True
    assert snapshot["rate_limit_enabled"] is True
    assert snapshot["rate_limit_requests"] == 2
    assert snapshot["active_rate_limit_keys"] >= 1
