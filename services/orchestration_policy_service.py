from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from config.settings import settings
from schemas.chat import ConversationContext
from schemas.orchestration import OrchestrationDecision
from services.ticketing_service import ticketing_service


@dataclass
class OrchestrationPolicyResult:
    """Deterministic policy-gate result for LLM orchestration actions."""

    allowed: bool = True
    blocked_reason: str = ""
    override_response: str = ""
    action_allowed: bool = True
    target_service: dict[str, Any] | None = None
    out_of_phase: bool = False
    current_phase_id: str = ""
    current_phase_name: str = ""
    service_phase_id: str = ""
    service_phase_name: str = ""
    ticket_create_allowed: bool = False
    ticket_skip_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class OrchestrationPolicyService:
    """Central deterministic checks for LLM-produced decisions."""

    @staticmethod
    def _normalize_identifier(value: Any) -> str:
        return str(value or "").strip().lower().replace(" ", "_")

    def _phase_label(self, phase_id: str, capabilities_summary: dict[str, Any]) -> str:
        normalized = self._normalize_identifier(phase_id)
        if not normalized:
            return ""
        phase_rows = capabilities_summary.get("journey_phases")
        if not isinstance(phase_rows, list):
            phase_rows = capabilities_summary.get("phases")
        if isinstance(phase_rows, list):
            for row in phase_rows:
                if not isinstance(row, dict):
                    continue
                rid = self._normalize_identifier(row.get("id"))
                if rid == normalized:
                    label = str(row.get("name") or "").strip()
                    if label:
                        return label
        return normalized.replace("_", " ").title()

    def _resolve_target_service(
        self,
        target_service_id: str,
        capabilities_summary: dict[str, Any],
    ) -> dict[str, Any] | None:
        sid = self._normalize_identifier(target_service_id)
        if not sid:
            return None
        services = capabilities_summary.get("service_catalog", [])
        if not isinstance(services, list):
            return None
        for service in services:
            if not isinstance(service, dict):
                continue
            if self._normalize_identifier(service.get("id")) == sid:
                return dict(service)
        return None

    @staticmethod
    def _coerce_bool(value: Any, *, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        if text in {"true", "1", "yes", "y"}:
            return True
        if text in {"false", "0", "no", "n"}:
            return False
        return default

    def _generic_kb_request_enabled(self) -> bool:
        return bool(getattr(settings, "chat_llm_generic_kb_ticketing_enabled", True))

    def _generic_kb_evidence(self, decision: OrchestrationDecision) -> list[str]:
        ticket = getattr(decision, "ticket", None)
        ticket_evidence = getattr(ticket, "evidence", None)
        if isinstance(ticket_evidence, list):
            return [str(item).strip() for item in ticket_evidence if str(item).strip()]
        metadata = decision.metadata if isinstance(decision.metadata, dict) else {}
        meta_evidence = metadata.get("generic_kb_evidence") or metadata.get("kb_evidence")
        if isinstance(meta_evidence, list):
            return [str(item).strip() for item in meta_evidence if str(item).strip()]
        return []

    def _is_generic_kb_request(self, decision: OrchestrationDecision) -> bool:
        if not self._generic_kb_request_enabled():
            return False
        ticket = getattr(decision, "ticket", None)
        if bool(getattr(ticket, "generic_request", False)):
            return True
        metadata = decision.metadata if isinstance(decision.metadata, dict) else {}
        return self._coerce_bool(
            metadata.get("generic_kb_request") or metadata.get("kb_grounded_request"),
            default=False,
        )

    def _generic_kb_phase_applicable(self, decision: OrchestrationDecision) -> bool:
        ticket = getattr(decision, "ticket", None)
        phase_applicable = getattr(ticket, "phase_applicable", None)
        if phase_applicable is not None:
            return bool(phase_applicable)
        metadata = decision.metadata if isinstance(decision.metadata, dict) else {}
        if "generic_kb_phase_applicable" in metadata:
            return self._coerce_bool(metadata.get("generic_kb_phase_applicable"), default=False)
        if "kb_phase_applicable" in metadata:
            return self._coerce_bool(metadata.get("kb_phase_applicable"), default=False)
        return False

    def _current_phase(
        self,
        *,
        context: ConversationContext,
        selected_phase_context: dict[str, Any] | None,
        capabilities_summary: dict[str, Any],
    ) -> tuple[str, str]:
        selected = selected_phase_context if isinstance(selected_phase_context, dict) else {}
        phase_id = self._normalize_identifier(selected.get("selected_phase_id"))
        phase_name = str(selected.get("selected_phase_name") or "").strip()
        if phase_id:
            if not phase_name:
                phase_name = self._phase_label(phase_id, capabilities_summary)
            return phase_id, phase_name

        pending_data = context.pending_data if isinstance(context.pending_data, dict) else {}
        integration = pending_data.get("_integration", {})
        if isinstance(integration, dict):
            candidate = self._normalize_identifier(integration.get("phase"))
            if candidate:
                return candidate, self._phase_label(candidate, capabilities_summary)

        fallback = "pre_booking"
        return fallback, self._phase_label(fallback, capabilities_summary)

    def _out_of_phase_response(
        self,
        *,
        service: dict[str, Any],
        current_phase_id: str,
        current_phase_name: str,
        service_phase_name: str,
        capabilities_summary: dict[str, Any],
    ) -> str:
        service_name = str(service.get("name") or service.get("id") or "this service").strip()
        current_name = str(current_phase_name or "the current phase").strip()
        service_name_phase = str(service_phase_name or "another phase").strip()
        current_phase_key = self._normalize_identifier(current_phase_id)

        alternatives: list[str] = []
        services = capabilities_summary.get("service_catalog", [])
        if isinstance(services, list):
            for row in services:
                if not isinstance(row, dict):
                    continue
                if not bool(row.get("is_active", True)):
                    continue
                phase_id = self._normalize_identifier(row.get("phase_id"))
                if current_phase_key and phase_id and phase_id != current_phase_key:
                    continue
                candidate_name = str(row.get("name") or row.get("id") or "").strip()
                if not candidate_name:
                    continue
                if candidate_name.lower() == service_name.lower():
                    continue
                alternatives.append(candidate_name)
                if len(alternatives) >= 3:
                    break
        alternatives_text = ", ".join(alternatives)
        if alternatives_text:
            alternatives_text = f" Right now I can help with: {alternatives_text}."

        return (
            f"{service_name} is available in {service_name_phase} phase. "
            f"You are currently in {current_name} phase.{alternatives_text}"
        ).strip()

    def evaluate(
        self,
        *,
        decision: OrchestrationDecision,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        selected_phase_context: dict[str, Any] | None = None,
    ) -> OrchestrationPolicyResult:
        result = OrchestrationPolicyResult()

        action = self._normalize_identifier(decision.action)
        target_service_id = self._normalize_identifier(decision.target_service_id)
        service = self._resolve_target_service(target_service_id, capabilities_summary)
        current_phase_id, current_phase_name = self._current_phase(
            context=context,
            selected_phase_context=selected_phase_context,
            capabilities_summary=capabilities_summary,
        )
        result.current_phase_id = current_phase_id
        result.current_phase_name = current_phase_name

        if service is not None:
            result.target_service = service
            result.service_phase_id = self._normalize_identifier(service.get("phase_id"))
            result.service_phase_name = self._phase_label(result.service_phase_id, capabilities_summary)

        # Action-level service resolution checks.
        service_required_actions = {"collect_info", "dispatch_handler", "create_ticket"}
        generic_kb_request = self._is_generic_kb_request(decision)
        generic_kb_evidence = self._generic_kb_evidence(decision)
        if action in service_required_actions and not service:
            if generic_kb_request and action in {"collect_info", "create_ticket"}:
                if not generic_kb_evidence:
                    result.allowed = False
                    result.action_allowed = False
                    result.blocked_reason = "generic_kb_evidence_missing"
                    result.override_response = (
                        "I do not have enough confirmed policy information to arrange that here yet. "
                        "If you want, I can connect you with our staff team."
                    )
                    return result
                if not self._generic_kb_phase_applicable(decision):
                    result.allowed = False
                    result.action_allowed = False
                    result.blocked_reason = "generic_kb_request_not_applicable_in_phase"
                    result.override_response = (
                        "I can share the hotel policy for that, but I cannot arrange it right now. "
                        "If you need help with something else, tell me what you need."
                    )
                    return result
                result.metadata["generic_kb_request"] = True
                result.metadata["generic_kb_evidence"] = list(generic_kb_evidence[:3])
            else:
                result.allowed = False
                result.action_allowed = False
                result.blocked_reason = "unknown_target_service"
                result.override_response = (
                    "I could not map this request to a configured service yet. "
                    "Please share which service you need, and I will continue."
                )
                return result

        if service is not None and not bool(service.get("is_active", True)):
            result.allowed = False
            result.action_allowed = False
            result.blocked_reason = "service_inactive"
            service_name = str(service.get("name") or service.get("id") or "This service").strip()
            result.override_response = (
                f"{service_name} is currently unavailable. "
                "Please share if you want help with another service."
            )
            return result

        # Phase checks for service-bound transactional actions.
        if service is not None and action in service_required_actions:
            service_phase_id = self._normalize_identifier(service.get("phase_id"))
            if current_phase_id and service_phase_id and current_phase_id != service_phase_id:
                result.allowed = False
                result.action_allowed = False
                result.out_of_phase = True
                result.blocked_reason = "phase_service_mismatch"
                result.override_response = self._out_of_phase_response(
                    service=service,
                    current_phase_id=current_phase_id,
                    current_phase_name=current_phase_name,
                    service_phase_name=result.service_phase_name or service_phase_id.replace("_", " ").title(),
                    capabilities_summary=capabilities_summary,
                )

        # Ticket creation checks.
        ticket_required = bool(decision.ticket.required or action == "create_ticket")
        if not ticket_required:
            return result

        if not bool(getattr(settings, "ticketing_plugin_enabled", True)):
            result.ticket_skip_reason = "ticketing_plugin_disabled"
            return result

        if not ticketing_service.is_ticketing_enabled(capabilities_summary):
            result.ticket_skip_reason = "ticketing_service_disabled"
            return result

        if service is not None and not bool(service.get("ticketing_enabled", True)):
            result.ticket_skip_reason = "phase_service_ticketing_disabled"
            return result

        if result.out_of_phase:
            result.ticket_skip_reason = "phase_service_mismatch"
            return result

        issue = str(decision.ticket.issue or "").strip()
        reason = str(decision.ticket.reason or "").strip()
        if bool(decision.ticket.ready_to_create) and not (issue or reason):
            # Auto-populate issue from pending_data as fallback before blocking
            pending = context.pending_data if isinstance(context.pending_data, dict) else {}
            room_type = str(pending.get("room_type") or pending.get("room_name") or "").strip()
            checkin = str(pending.get("stay_checkin_date") or pending.get("checkin_date") or "").strip()
            checkout = str(pending.get("stay_checkout_date") or pending.get("checkout_date") or "").strip()
            guests = str(pending.get("guest_count") or pending.get("guests") or "").strip()
            service_name = str((result.target_service or {}).get("name") or decision.target_service_id or "").strip()
            if not service_name and generic_kb_request:
                service_name = str(
                    pending.get("generic_kb_issue")
                    or pending.get("generic_kb_reason")
                    or pending.get("request_summary")
                    or pending.get("request_type")
                    or "request"
                ).strip()
            if not service_name:
                service_name = "service"
            fallback_parts = [service_name]
            if room_type:
                fallback_parts.append(room_type)
            if checkin:
                fallback_parts.append(f"check-in {checkin}")
            if checkout:
                fallback_parts.append(f"check-out {checkout}")
            if guests:
                fallback_parts.append(f"{guests} guests")
            auto_issue = ", ".join(fallback_parts)
            if auto_issue and auto_issue != service_name:
                decision.ticket.issue = auto_issue
            else:
                result.ticket_skip_reason = "ticket_issue_missing"
                return result

        result.ticket_create_allowed = True
        return result


orchestration_policy_service = OrchestrationPolicyService()
