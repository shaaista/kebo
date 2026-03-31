"""
Business Configuration Service

Loads, saves, and manages business configuration files.
Supports multiple industries with template-based setup.
"""

import copy
import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional, Dict, Any, List
from uuid import uuid4
from pydantic import BaseModel, Field


# Config directory paths
CONFIG_DIR = Path(__file__).parent.parent / "config"
TEMPLATES_DIR = CONFIG_DIR / "templates"
PROMPT_TEMPLATES_DIR = CONFIG_DIR / "prompt_templates"
BUSINESS_CONFIG_FILE = CONFIG_DIR / "business_config.json"

_KB_CONFLICT_AVAILABLE_MARKERS = (
    "available",
    "open",
    "operating",
    "operates",
    "24/7",
    "provides",
    "provide",
    "offers",
    "serves",
)
_KB_CONFLICT_UNAVAILABLE_MARKERS = (
    "unavailable",
    "not available",
    "temporarily closed",
    "closed",
    "suspended",
    "not operational",
)
_KB_CONFLICT_ROOM_DELIVERY_MARKERS = (
    "room delivery",
    "deliver to room",
    "delivered to room",
    "in-room delivery",
    "in room delivery",
    "to your room",
)
_KB_CONFLICT_DINE_IN_ONLY_MARKERS = (
    "dine-in only",
    "dine in only",
    "not for room delivery",
    "no room delivery",
    "dine in",
)

_LIBRARY_TOPIC_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "this",
    "that",
    "your",
    "hotel",
    "guest",
    "guests",
    "service",
    "services",
    "section",
    "info",
    "information",
    "details",
}

_PREBOOKING_TICKETING_CASES = [
    "Pre-booking guest asks for human sales callback or manual follow-up.",
    "Pre-booking room discovery request needs custom/manual room arrangement or non-standard requirement.",
    "Pre-booking rate enquiry needs custom quotation, corporate pricing, group pricing, or manual discount approval.",
    "Pre-booking policy enquiry requires exception approval by staff.",
    "Pre-booking website booking issue remains unresolved (OTP/login/form/technical error).",
    "Pre-booking payment issue: failed, pending, duplicate debit, or confirmation mismatch.",
    "Sales callback request captured during pre-booking.",
    "Group/corporate booking enquiry requiring manual proposal support.",
]

_PRECHECKIN_TICKETING_CASES = [
    "Pre-checkin booking status request has mismatch/not-found booking and needs manual verification.",
    "Pre-checkin booking modification needs staff/PMS update or approval.",
    "Pre-checkin booking cancellation needs manual cancellation execution, refund follow-up, or exception approval.",
    "Pre-checkin pre-arrival coordination requires operational handoff.",
    "Early check-in request requires availability check and approval.",
    "Airport transfer request requires manual transport coordination.",
    "Pre-checkin document validation failed or needs manual verification/override.",
    "Special occasion or amenity setup before arrival requires operations task ownership.",
]

_DURING_STAY_TICKETING_CASES = [
    "During-stay housekeeping request requires staff action.",
    "During-stay maintenance issue (AC/electrical/plumbing/device) requires engineering intervention.",
    "During-stay in-room dining request or order issue requires staff fulfillment/follow-up.",
    "During-stay restaurant reservation request requires outlet staff confirmation.",
    "During-stay spa/recreation booking request requires staff confirmation.",
    "During-stay transport request requires dispatch coordination.",
    "During-stay complaint/dissatisfaction requires staff resolution or escalation.",
    "During-stay late checkout or stay extension requires front office approval.",
    "During-stay front desk assistance request needs manual intervention.",
    "During-stay emergency/safety/medical/security issue needs immediate escalation.",
]

_POSTCHECKOUT_TICKETING_CASES = [
    "Post-checkout invoice or billing clarification needs finance/front office follow-up.",
    "Post-checkout refund, security deposit, or charge reversal request needs manual verification.",
    "Post-checkout lost-and-found request requires item search, verification, or courier coordination.",
    "Post-checkout complaint or negative feedback requires service recovery ownership.",
    "Post-checkout tax invoice/GST correction request needs manual document reissue.",
    "Post-checkout loyalty points or membership benefit correction needs manual support.",
    "Post-checkout stay confirmation, folio copy, or receipt resend failed and needs staff action.",
    "Post-checkout rebooking callback request needs sales/reservations follow-up.",
]

_DEFAULT_TICKETING_CASES = [
    *_PREBOOKING_TICKETING_CASES,
    *_PRECHECKIN_TICKETING_CASES,
    *_DURING_STAY_TICKETING_CASES,
    *_POSTCHECKOUT_TICKETING_CASES,
    "Guest reports a complaint or maintenance issue that requires staff action.",
    "Guest requests human escalation or live agent support.",
    "A booking or order requires manual staff follow-up after final confirmation.",
    "Table booking request requires staff support.",
    "In-room dining food order requires staff action.",
    "Spa booking requires staff confirmation.",
    "Room booking requires staff confirmation.",
    "Transport/pickup request that requires staff coordination.",
    "Information not available in provided data/context; create a follow-up ticket.",
    "Requested menu is unavailable and needs team follow-up.",
    "Pre-booking website issue, payment issue, or quotation issue.",
    "Booking assistance update, modify, or follow-up request.",
    "Sightseeing directions unavailable or unclear; route to Guest Relations.",
    "Generic booking-help request requiring staff support.",
    "Special immediate requests like birthday cake or shaving gel.",
]

_DEFAULT_JOURNEY_PHASES = [
    {
        "id": "pre_booking",
        "name": "Pre Booking",
        "description": "Guest is exploring and asking questions before reservation confirmation.",
        "is_active": True,
        "order": 1,
    },
    {
        "id": "pre_checkin",
        "name": "Pre Checkin",
        "description": "Guest booking is confirmed and needs support before arrival.",
        "is_active": True,
        "order": 2,
    },
    {
        "id": "during_stay",
        "name": "During Stay",
        "description": "Guest is in-house and needs operational or service support.",
        "is_active": True,
        "order": 3,
    },
    {
        "id": "post_checkout",
        "name": "Post Checkout",
        "description": "Guest has checked out and needs follow-up assistance.",
        "is_active": True,
        "order": 4,
    },
]

_PREBOOKING_PREBUILT_SERVICES = [
]

_PRECHECKIN_PREBUILT_SERVICES = [
]

_DURINGSTAY_PREBUILT_SERVICES = [
]

_POSTCHECKOUT_PREBUILT_SERVICES = [
]

_PHASE_PREBUILT_SERVICES: dict[str, list[dict[str, Any]]] = {
    "pre_booking": _PREBOOKING_PREBUILT_SERVICES,
    "pre_checkin": _PRECHECKIN_PREBUILT_SERVICES,
    "during_stay": _DURINGSTAY_PREBUILT_SERVICES,
    "post_checkout": _POSTCHECKOUT_PREBUILT_SERVICES,
}


class BusinessInfo(BaseModel):
    """Business basic information."""
    id: str
    name: str
    type: str  # hotel, retail, healthcare, etc.
    city: str
    timezone: str = "Asia/Kolkata"
    currency: str = "INR"
    language: str = "en"
    bot_name: str = "Assistant"
    welcome_message: str = "Hello! How can I help you today?"


class Capability(BaseModel):
    """Single capability configuration."""
    enabled: bool = True
    description: str = ""
    hours: Optional[str] = None
    window_days: Optional[int] = None


class Service(BaseModel):
    """Service/department configuration."""
    id: str
    name: str
    type: str  # service category (department, clinic, outlet, etc.)
    description: Optional[str] = None
    cuisine: Optional[str] = None
    hours: Optional[Dict[str, str]] = None
    delivery_zones: Optional[List[str]] = None
    is_active: bool = True


class FAQEntry(BaseModel):
    """Admin-managed predefined FAQ question and answer."""
    id: str
    question: str
    answer: str
    description: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    enabled: bool = True


class ToolConfig(BaseModel):
    """Admin-managed tool/workflow toggle."""
    id: str
    name: str
    description: Optional[str] = None
    type: str = "workflow"
    handler: Optional[str] = None
    channels: List[str] = Field(default_factory=list)
    enabled: bool = True
    requires_confirmation: bool = False
    ticketing_plugin_enabled: Optional[bool] = None
    ticketing_cases: List[str] = Field(default_factory=list)


class Intent(BaseModel):
    """Intent configuration."""
    id: str
    label: str
    enabled: bool = True


class EscalationConfig(BaseModel):
    """Escalation settings."""
    confidence_threshold: float = 0.4
    max_clarification_attempts: int = 3
    escalation_message: str = "Let me connect you with our team."
    modes: List[str] = ["live_chat", "ticket"]


class BusinessConfig(BaseModel):
    """Complete business configuration."""
    business: BusinessInfo
    capabilities: Dict[str, Capability]
    services: List[Service]
    faq_bank: List[FAQEntry]
    tools: List[ToolConfig]
    intents: List[Intent]
    escalation: EscalationConfig
    prompts: Optional[Dict[str, str]] = None
    ui_settings: Optional[Dict[str, str]] = None


class ConfigService:
    """Service for managing business configurations."""

    def __init__(self):
        self._config: Optional[Dict[str, Any]] = None
        self._config_mtime: Optional[float] = None
        self._config_file: Optional[Path] = None
        self._ensure_directories()

    def _ensure_directories(self):
        """Ensure config directories exist."""
        CONFIG_DIR.mkdir(exist_ok=True)
        (CONFIG_DIR / "properties").mkdir(parents=True, exist_ok=True)
        TEMPLATES_DIR.mkdir(exist_ok=True)
        PROMPT_TEMPLATES_DIR.mkdir(exist_ok=True)

    def _resolve_scoped_business_id(self) -> str:
        try:
            from services.db_config_service import db_config_service

            scoped_code = db_config_service.get_current_hotel_code()
            normalized = self._normalize_identifier(scoped_code)
            if normalized:
                return normalized
        except Exception:
            pass
        return "default"

    def _resolve_config_file(self) -> Path:
        scoped_id = self._resolve_scoped_business_id()
        if scoped_id and scoped_id != "default":
            return CONFIG_DIR / "properties" / f"{scoped_id}.json"
        return BUSINESS_CONFIG_FILE

    def _default_config(self) -> Dict[str, Any]:
        """Canonical default config shape used for backward-compatible upgrades."""
        return {
            "business": {
                "id": "default",
                "name": "My Business",
                "type": "custom",
                "city": "City",
                "location": "",
                "address": "",
                "timezone": "Asia/Kolkata",
                "currency": "INR",
                "language": "en",
                "timestamp_format": "24h",
                "bot_name": "Assistant",
                "welcome_message": "Hello! How can I help you today?",
                "contact_email": "",
                "contact_phone": "",
                "website": "",
                "channels": {
                    "web_widget": True,
                    "whatsapp": True,
                },
            },
            "capabilities": {},
            "journey_phases": copy.deepcopy(_DEFAULT_JOURNEY_PHASES),
            "services": [],
            "faq_bank": [],
            "tools": [
                {
                    "id": "ticketing",
                    "name": "Ticketing",
                    "description": "Create support tickets for unresolved user requests.",
                    "type": "workflow",
                    "handler": "ticket_create",
                    "channels": ["web_widget", "whatsapp"],
                    "enabled": True,
                    "requires_confirmation": False,
                    "ticketing_plugin_enabled": True,
                    "ticketing_cases": list(_DEFAULT_TICKETING_CASES),
                },
                {
                    "id": "human_handoff",
                    "name": "Human Handoff",
                    "description": "Escalate the conversation to a human agent.",
                    "type": "handoff",
                    "handler": "human_escalation",
                    "channels": ["web_widget", "whatsapp"],
                    "enabled": True,
                    "requires_confirmation": False,
                },
            ],
            "intents": [],
            "escalation": {
                "confidence_threshold": 0.4,
                "max_clarification_attempts": 3,
                "escalation_message": "Let me connect you with our team.",
                "modes": ["live_chat", "ticket"],
            },
            "prompts": {
                "template_id": "generic_assistant",
                "system_prompt": "",
                "classifier_prompt": "",
                "response_style": "",
            },
            "knowledge_base": {
                "sources": [],
                "notes": "",
                "nlu_policy": {
                    "dos": [],
                    "donts": [],
                    "capability_constraints": {},
                },
                "library_index": {
                    "version": "v1",
                    "source_signature": "",
                    "generated_at": "",
                    "source_count": 0,
                    "documents": [],
                    "pages": [],
                    "books": [],
                    "coverage": {
                        "total_pages": 0,
                        "covered_pages": 0,
                        "uncovered_pages": 0,
                        "coverage_ratio": 0.0,
                    },
                },
            },
            "ui_settings": {
                "theme": {
                    "primary_color": "#2563eb",
                    "accent_color": "#22c55e",
                    "background_color": "#f8fafc",
                    "text_color": "#1e293b",
                },
                "widget": {
                    "position": "right",
                    "show_branding": True,
                    "compact_mode": False,
                },
                "channels": {
                    "web_widget": {"enabled": True},
                    "whatsapp": {"enabled": True},
                },
                "industry_features": [],
            },
            "agent_plugins": {
                "enabled": True,
                "shared_context": True,
                "strict_mode": True,
                "strict_unavailable_response": (
                    "I can only help with configured service-agent data right now. "
                    "Please contact staff for anything outside this scope."
                ),
                "plugins": [],
            },
            "service_kb": {
                "records": [],
                "compiler": {
                    "enabled": True,
                    "max_facts_per_service": 60,
                    "max_source_chars": 220000,
                    "max_sources": 25,
                    "version": "v1",
                },
            },
            "service_agent_releases": [],
            "runtime": {
                # Runtime-level feature switches for deterministic handlers.
                "menu_runtime_enabled": False,
                "service_kb_auto_compile": True,
            },
        }

    def _merge_defaults(self, target: Dict[str, Any], defaults: Dict[str, Any]) -> bool:
        """Recursively add missing default keys without overwriting user values."""
        changed = False
        for key, default_value in defaults.items():
            if key not in target:
                target[key] = copy.deepcopy(default_value)
                changed = True
                continue

            current_value = target.get(key)
            if isinstance(default_value, dict) and isinstance(current_value, dict):
                if self._merge_defaults(current_value, default_value):
                    changed = True
        return changed

    @staticmethod
    def _normalize_identifier(value: Any) -> str:
        """Normalize IDs to stable lowercase snake-style identifiers."""
        return str(value or "").strip().lower().replace(" ", "_")

    @classmethod
    def _normalize_phase_identifier(cls, value: Any) -> str:
        """Normalize phase IDs and map legacy aliases to canonical values."""
        normalized = cls._normalize_identifier(value)
        aliases = {
            "prebooking": "pre_booking",
            "booking": "pre_checkin",
            "precheckin": "pre_checkin",
            "duringstay": "during_stay",
            "instay": "during_stay",
            "in_stay": "during_stay",
            "postcheckout": "post_checkout",
        }
        return aliases.get(normalized, normalized)

    @staticmethod
    def _normalize_slug(value: Any) -> str:
        """Normalize free-form text to a stable slug."""
        lowered = str(value or "").strip().lower()
        slug = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
        return slug

    @classmethod
    def _normalize_channel_identifier(cls, value: Any) -> str:
        """Normalize channel IDs to canonical runtime values."""
        normalized = cls._normalize_identifier(value)
        if normalized in {"web", "web_widget", "widget", "chat_widget"}:
            return "web"
        if normalized in {"wa", "whatsapp", "whats_app"}:
            return "whatsapp"
        return normalized

    @classmethod
    def _service_prompt_profile_tokens(cls, service: Dict[str, Any]) -> set[str]:
        parts = [
            service.get("id"),
            service.get("name"),
            service.get("type"),
            service.get("description"),
            service.get("phase_id"),
        ]
        blob = " ".join(str(part or "") for part in parts).lower()
        return {token for token in re.findall(r"[a-z0-9]+", blob) if token}

    @classmethod
    def _infer_service_prompt_profile(cls, service: Dict[str, Any]) -> str:
        return cls._normalize_identifier(service.get("profile"))

    @classmethod
    def _coerce_service_prompt_slot(cls, slot: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(slot, dict):
            return None
        slot_id = cls._normalize_identifier(slot.get("id")) or cls._normalize_slug(slot.get("label"))
        if not slot_id:
            return None
        label = str(slot.get("label") or slot_id.replace("_", " ").title()).strip()
        prompt = str(slot.get("prompt") or f"Please share {slot_id.replace('_', ' ')}.").strip()
        slot_type = cls._normalize_identifier(slot.get("type") or "text")
        if slot_type not in {"text", "number", "date", "time", "datetime", "boolean", "enum"}:
            slot_type = "text"
        normalized_slot: Dict[str, Any] = {
            "id": slot_id,
            "label": label,
            "prompt": prompt,
            "required": bool(slot.get("required", True)),
            "type": slot_type,
        }
        options = slot.get("options")
        if isinstance(options, list):
            normalized_options: list[str] = []
            for option in options:
                option_text = str(option or "").strip()
                if option_text and option_text not in normalized_options:
                    normalized_options.append(option_text)
            if normalized_options:
                normalized_slot["options"] = normalized_options
        return normalized_slot

    @classmethod
    def _default_service_prompt_slots(cls, profile: str) -> list[dict[str, Any]]:
        _ = profile
        return []

    @classmethod
    def _default_service_prompt_validation_rules(cls, required_slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
        required_slot_ids = [
            str(slot.get("id") or "").strip()
            for slot in required_slots
            if isinstance(slot, dict) and bool(slot.get("required", True))
        ]
        required_slot_ids = [slot_id for slot_id in required_slot_ids if slot_id]
        rules: list[dict[str, Any]] = []
        if required_slot_ids:
            rules.append(
                {
                    "id": "required_slots_complete",
                    "type": "required",
                    "slot_ids": required_slot_ids,
                    "error_message": "Please complete all required details before confirmation.",
                }
            )
        if {"check_in_date", "check_out_date"}.issubset(set(required_slot_ids)):
            rules.append(
                {
                    "id": "checkin_before_checkout",
                    "type": "date_order",
                    "slot_ids": ["check_in_date", "check_out_date"],
                    "error_message": "Check-out date must be after check-in date.",
                }
            )
        numeric_slots = [slot_id for slot_id in ("guest_count", "passenger_count", "quantity") if slot_id in required_slot_ids]
        if numeric_slots:
            rules.append(
                {
                    "id": "numeric_values_positive",
                    "type": "numeric_min",
                    "slot_ids": numeric_slots,
                    "min_value": 1,
                    "error_message": "Please provide a valid positive number.",
                }
            )
        return rules

    @classmethod
    def _generate_service_prompt_pack(cls, service: Dict[str, Any]) -> Dict[str, Any]:
        service_name = str(service.get("name") or service.get("id") or "service").strip()
        phase_id = cls._normalize_phase_identifier(service.get("phase_id"))
        profile = str(service.get("profile") or "").strip().lower()
        ticketing_enabled = bool(service.get("ticketing_enabled", True))
        ticketing_policy = str(service.get("ticketing_policy") or "").strip()

        return {
            "version": 2,
            "generator": "service_prompt_pack_v2",
            "source": "system_default",
            "profile": profile,
            "role": f"You are the dedicated assistant for {service_name}.",
            "professional_behavior": (
                "Use only admin-configured policy and KB evidence. "
                "Ask concise clarifying questions when required details are missing. "
                "Do not invent fixed field schemas."
            ),
            "phase_id": phase_id,
            "required_slots": [],
            "validation_rules": [],
            "confirmation_format": {
                "style": "summary_then_explicit_confirm",
                "template": (
                    "Please confirm these details before execution: {summary}. "
                    "Reply 'yes confirm' to proceed or share corrections."
                ),
                "required_phrase": "yes confirm",
            },
            "ticketing_policy": {
                "enabled": ticketing_enabled,
                "policy": ticketing_policy,
                "decision_template": "",
            },
            "execution_guard": {},
        }

    @classmethod
    def _normalize_service_prompt_pack(
        cls,
        pack: Any,
        *,
        service: Dict[str, Any],
        source: str = "manual_override",
    ) -> Dict[str, Any]:
        generated = cls._generate_service_prompt_pack(service)
        if not isinstance(pack, dict):
            return generated

        normalized = copy.deepcopy(generated)
        for key in ("role", "professional_behavior", "profile"):
            value = str(pack.get(key) or "").strip()
            if value:
                normalized[key] = value

        # If the saved pack has ticketing_conditions, the admin has explicitly defined
        # what to collect via that text. Wipe the auto-generated default slots so they
        # don't override the admin's intent.
        if str(pack.get("ticketing_conditions") or "").strip():
            normalized["required_slots"] = []

        required_slots_raw = pack.get("required_slots")
        if isinstance(required_slots_raw, list):
            normalized_slots: list[dict[str, Any]] = []
            seen_slot_ids: set[str] = set()
            for item in required_slots_raw:
                normalized_slot = cls._coerce_service_prompt_slot(item)
                if not normalized_slot:
                    continue
                slot_id = normalized_slot["id"]
                if slot_id in seen_slot_ids:
                    continue
                seen_slot_ids.add(slot_id)
                normalized_slots.append(normalized_slot)
            if normalized_slots:
                normalized["required_slots"] = normalized_slots
                normalized["validation_rules"] = cls._default_service_prompt_validation_rules(normalized_slots)

        validation_rules_raw = pack.get("validation_rules")
        if isinstance(validation_rules_raw, list):
            normalized_rules: list[dict[str, Any]] = []
            for rule in validation_rules_raw:
                if not isinstance(rule, dict):
                    continue
                rule_id = cls._normalize_identifier(rule.get("id"))
                rule_type = cls._normalize_identifier(rule.get("type"))
                if not rule_id or not rule_type:
                    continue
                slot_ids_raw = rule.get("slot_ids", [])
                if not isinstance(slot_ids_raw, list):
                    slot_ids_raw = []
                slot_ids = [cls._normalize_identifier(slot_id) for slot_id in slot_ids_raw]
                slot_ids = [slot_id for slot_id in slot_ids if slot_id]
                normalized_rule: Dict[str, Any] = {
                    "id": rule_id,
                    "type": rule_type,
                    "slot_ids": slot_ids,
                    "error_message": str(rule.get("error_message") or "").strip(),
                }
                if "min_value" in rule:
                    try:
                        normalized_rule["min_value"] = float(rule.get("min_value"))
                    except (TypeError, ValueError):
                        pass
                normalized_rules.append(normalized_rule)
            if normalized_rules:
                normalized["validation_rules"] = normalized_rules

        confirmation = pack.get("confirmation_format")
        if isinstance(confirmation, dict):
            normalized_confirmation = dict(normalized.get("confirmation_format", {}))
            style_value = str(confirmation.get("style") or "").strip()
            template_value = str(confirmation.get("template") or "").strip()
            phrase_value = str(confirmation.get("required_phrase") or "").strip()
            if style_value:
                normalized_confirmation["style"] = style_value
            if template_value:
                normalized_confirmation["template"] = template_value
            if phrase_value:
                normalized_confirmation["required_phrase"] = phrase_value
            normalized["confirmation_format"] = normalized_confirmation

        ticketing = pack.get("ticketing_policy")
        if isinstance(ticketing, dict):
            normalized_ticketing = dict(normalized.get("ticketing_policy", {}))
            if "enabled" in ticketing:
                normalized_ticketing["enabled"] = bool(ticketing.get("enabled"))
            policy_value = str(ticketing.get("policy") or "").strip()
            if policy_value:
                normalized_ticketing["policy"] = policy_value
            decision_template = str(ticketing.get("decision_template") or "").strip()
            if decision_template:
                normalized_ticketing["decision_template"] = decision_template
            normalized["ticketing_policy"] = normalized_ticketing

        execution_guard = pack.get("execution_guard")
        if isinstance(execution_guard, dict):
            normalized_guard = dict(normalized.get("execution_guard", {}))
            if "require_required_slots_before_confirm" in execution_guard:
                normalized_guard["require_required_slots_before_confirm"] = bool(
                    execution_guard.get("require_required_slots_before_confirm")
                )
            normalized["execution_guard"] = normalized_guard

        # Preserve free-text fields that are not part of structural validation
        passthrough: dict[str, str] = {}
        for passthrough_key in ("extracted_knowledge", "ticketing_conditions"):
            value = str(pack.get(passthrough_key) or "").strip()
            if value:
                normalized[passthrough_key] = value
                passthrough[passthrough_key] = value

        try:
            normalized["version"] = max(1, int(pack.get("version") or normalized.get("version") or 1))
        except (TypeError, ValueError):
            normalized["version"] = 1
        normalized["source"] = str(source or "manual_override")
        if not cls._is_valid_service_prompt_pack(normalized):
            # Still carry passthrough fields even when falling back to generated defaults
            generated.update(passthrough)
            return generated
        return normalized

    @classmethod
    def _is_valid_service_prompt_pack(cls, pack: Any) -> bool:
        if not isinstance(pack, dict):
            return False
        required_slots = pack.get("required_slots")
        if not isinstance(required_slots, list):
            return False
        for slot in required_slots:
            if not isinstance(slot, dict):
                return False
            slot_id = cls._normalize_identifier(slot.get("id"))
            prompt = str(slot.get("prompt") or "").strip()
            if not slot_id or not prompt:
                return False
        confirmation = pack.get("confirmation_format")
        if not isinstance(confirmation, dict):
            return False
        if not str(confirmation.get("template") or "").strip():
            return False
        ticketing = pack.get("ticketing_policy")
        if not isinstance(ticketing, dict):
            return False
        if "enabled" not in ticketing:
            return False
        return True

    @classmethod
    def _normalize_service_entry(
        cls,
        service: Dict[str, Any],
        *,
        manual_prompt_override: bool = False,
        preserve_manual_prompt_pack: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Normalize one service entry from admin inputs."""
        if not isinstance(service, dict):
            return None

        service_id = cls._normalize_identifier(service.get("id"))
        if not service_id:
            return None

        normalized = dict(service)
        normalized["id"] = service_id
        normalized["name"] = str(service.get("name") or service_id).strip()
        normalized["type"] = cls._normalize_identifier(service.get("type") or "service")
        normalized["description"] = str(service.get("description") or "").strip()
        normalized["cuisine"] = str(service.get("cuisine") or "").strip() or None
        phase_id = cls._normalize_phase_identifier(service.get("phase_id"))
        if phase_id:
            normalized["phase_id"] = phase_id
        elif "phase_id" in normalized:
            normalized.pop("phase_id", None)
        if "ticketing_enabled" in service:
            normalized["ticketing_enabled"] = bool(service.get("ticketing_enabled"))
        normalized["is_builtin"] = bool(service.get("is_builtin", False))
        hours_value = service.get("hours")
        if isinstance(hours_value, dict):
            open_value = str(hours_value.get("open") or "").strip()
            close_value = str(hours_value.get("close") or "").strip()
            normalized["hours"] = {"open": open_value, "close": close_value} if (open_value or close_value) else {}
        else:
            normalized["hours"] = {}

        delivery_zones_value = service.get("delivery_zones")
        normalized_zones: list[str] = []
        if isinstance(delivery_zones_value, list):
            for zone in delivery_zones_value:
                zone_str = str(zone or "").strip().lower()
                if zone_str and zone_str not in normalized_zones:
                    normalized_zones.append(zone_str)
        elif isinstance(delivery_zones_value, str):
            zone_str = delivery_zones_value.strip().lower()
            if zone_str:
                normalized_zones = [zone_str]
        normalized["delivery_zones"] = normalized_zones
        normalized["is_active"] = bool(service.get("is_active", True))
        normalized["ticketing_policy"] = str(service.get("ticketing_policy") or "").strip()

        # --- ticketing_mode & form_config ---
        # ticketing_mode: "form" | "text" | "none"
        raw_ticketing_mode = str(service.get("ticketing_mode") or "").strip().lower()
        if raw_ticketing_mode in ("form", "text", "none"):
            normalized["ticketing_mode"] = raw_ticketing_mode
        else:
            # Backward compat: derive from ticketing_enabled
            normalized["ticketing_mode"] = "text" if normalized.get("ticketing_enabled") else "none"

        form_config = service.get("form_config")
        if isinstance(form_config, dict):
            # Normalize form_config
            norm_fc: Dict[str, Any] = {}
            # trigger_field
            tf = form_config.get("trigger_field")
            if isinstance(tf, dict) and tf.get("id"):
                norm_fc["trigger_field"] = {
                    "id": str(tf["id"]).strip(),
                    "label": str(tf.get("label") or tf["id"]).strip(),
                    "description": str(tf.get("description") or "").strip(),
                }
            # fields
            raw_fields = form_config.get("fields")
            if isinstance(raw_fields, list):
                norm_fields = []
                for f in raw_fields:
                    if not isinstance(f, dict):
                        continue
                    fid = str(f.get("id") or "").strip()
                    if not fid:
                        continue
                    norm_fields.append({
                        "id": fid,
                        "label": str(f.get("label") or fid).strip(),
                        "type": str(f.get("type") or "text").strip(),
                        "required": bool(f.get("required", True)),
                        "validation_prompt": str(f.get("validation_prompt") or "").strip(),
                    })
                norm_fc["fields"] = norm_fields
            else:
                norm_fc["fields"] = []
            norm_fc["pre_form_instructions"] = str(form_config.get("pre_form_instructions") or "").strip()
            normalized["form_config"] = norm_fc
        elif "form_config" not in normalized:
            # Only set default if not already present on the service
            if normalized["ticketing_mode"] == "form":
                normalized["form_config"] = {"trigger_field": {}, "fields": [], "pre_form_instructions": ""}
            # For text/none modes, don't add form_config to keep config clean

        existing_prompt_pack = service.get("service_prompt_pack")
        existing_source = ""
        if isinstance(existing_prompt_pack, dict):
            existing_source = str(existing_prompt_pack.get("source") or "").strip().lower()
        existing_custom_flag = bool(service.get("service_prompt_pack_custom", False))
        existing_manual = existing_custom_flag or existing_source in {
            "manual_override",
            "admin_ui",
            "admin_override",
            "db",
        }

        prompt_pack: Dict[str, Any]
        if manual_prompt_override and isinstance(existing_prompt_pack, dict):
            prompt_pack = cls._normalize_service_prompt_pack(
                existing_prompt_pack,
                service=normalized,
                source="manual_override",
            )
            prompt_pack_custom = True
        elif preserve_manual_prompt_pack and existing_manual and isinstance(existing_prompt_pack, dict):
            prompt_pack = cls._normalize_service_prompt_pack(
                existing_prompt_pack,
                service=normalized,
                source="manual_override",
            )
            prompt_pack_custom = True
        else:
            prompt_pack = cls._generate_service_prompt_pack(normalized)
            prompt_pack_custom = False

        if not cls._is_valid_service_prompt_pack(prompt_pack):
            prompt_pack = cls._generate_service_prompt_pack(normalized)
            prompt_pack_custom = False
        if str(prompt_pack.get("source") or "").strip().lower() != "manual_override":
            prompt_pack_custom = False
        normalized["service_prompt_pack"] = prompt_pack
        normalized["service_prompt_pack_custom"] = prompt_pack_custom
        return normalized

    @classmethod
    def _normalize_phase_entry(cls, phase: Dict[str, Any], default_order: int = 0) -> Optional[Dict[str, Any]]:
        """Normalize one phase entry from admin inputs."""
        if not isinstance(phase, dict):
            return None

        raw_phase_id = cls._normalize_identifier(phase.get("id"))
        phase_id = cls._normalize_phase_identifier(phase.get("id"))
        if not phase_id:
            return None

        name = str(phase.get("name") or phase_id.replace("_", " ").title()).strip()
        description = str(phase.get("description") or "").strip()
        if raw_phase_id == "booking":
            if not str(phase.get("name") or "").strip() or str(phase.get("name") or "").strip().lower() == "booking":
                name = "Pre Checkin"
            if not description or "reservation/payment/modify/cancel" in description.lower():
                description = "Guest booking is confirmed and needs support before arrival."
        try:
            order_value = int(phase.get("order", default_order))
        except Exception:
            order_value = int(default_order)

        return {
            "id": phase_id,
            "name": name,
            "description": description,
            "is_active": bool(phase.get("is_active", True)),
            "order": order_value,
        }

    @classmethod
    def _normalize_intent_entry(cls, intent: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Normalize one intent entry from admin inputs."""
        if not isinstance(intent, dict):
            return None

        intent_id = cls._normalize_identifier(intent.get("id"))
        if not intent_id:
            return None

        normalized: Dict[str, Any] = {
            "id": intent_id,
            "label": str(intent.get("label") or intent_id.replace("_", " ").title()).strip(),
            "enabled": bool(intent.get("enabled", True)),
        }
        maps_to = cls._normalize_identifier(intent.get("maps_to"))
        if maps_to and maps_to != intent_id:
            normalized["maps_to"] = maps_to
        return normalized

    @classmethod
    def _normalize_faq_entry(cls, faq: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Normalize one FAQ entry from admin inputs."""
        if not isinstance(faq, dict):
            return None

        question = str(faq.get("question") or "").strip()
        answer = str(faq.get("answer") or "").strip()
        if not question or not answer:
            return None

        faq_id = cls._normalize_identifier(faq.get("id")) or cls._normalize_slug(question)
        if not faq_id:
            return None

        tags = faq.get("tags", [])
        if not isinstance(tags, list):
            tags = []

        normalized_tags = []
        for tag in tags:
            normalized_tag = cls._normalize_slug(tag)
            if normalized_tag and normalized_tag not in normalized_tags:
                normalized_tags.append(normalized_tag)

        return {
            "id": faq_id,
            "question": question,
            "answer": answer,
            "description": str(faq.get("description") or "").strip(),
            "tags": normalized_tags,
            "enabled": bool(faq.get("enabled", True)),
        }

    @classmethod
    def _normalize_ticketing_cases(cls, raw_cases: Any) -> list[str]:
        """Normalize ticketing-case rows from either strings or object entries."""
        if not isinstance(raw_cases, list):
            return []

        cleaned: list[str] = []
        for item in raw_cases:
            if isinstance(item, dict):
                text = str(item.get("description") or item.get("case") or item.get("label") or "").strip()
            else:
                text = str(item or "").strip()
            if not text:
                continue
            normalized = re.sub(r"\s+", " ", text)
            if normalized and normalized not in cleaned:
                cleaned.append(normalized)
        return cleaned[:40]

    @classmethod
    def _normalize_tool_entry(cls, tool: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Normalize one tool entry from admin inputs."""
        if not isinstance(tool, dict):
            return None

        tool_id = cls._normalize_identifier(tool.get("id")) or cls._normalize_slug(tool.get("name"))
        if not tool_id:
            return None

        channels = tool.get("channels", [])
        if not isinstance(channels, list):
            channels = []
        normalized_channels = []
        for channel in channels:
            channel_id = cls._normalize_identifier(channel)
            if channel_id and channel_id not in normalized_channels:
                normalized_channels.append(channel_id)

        normalized_tool: Dict[str, Any] = {
            "id": tool_id,
            "name": str(tool.get("name") or tool_id.replace("_", " ").title()).strip(),
            "description": str(tool.get("description") or "").strip(),
            "type": cls._normalize_identifier(tool.get("type") or "workflow"),
            "handler": str(tool.get("handler") or "").strip() or None,
            "channels": normalized_channels,
            "enabled": bool(tool.get("enabled", True)),
            "requires_confirmation": bool(tool.get("requires_confirmation", False)),
        }
        if "ticketing_plugin_enabled" in tool:
            normalized_tool["ticketing_plugin_enabled"] = bool(tool.get("ticketing_plugin_enabled", True))

        ticketing_cases = cls._normalize_ticketing_cases(tool.get("ticketing_cases"))
        if ticketing_cases:
            normalized_tool["ticketing_cases"] = ticketing_cases

        return normalized_tool

    @classmethod
    def _normalize_agent_slot_entry(cls, slot: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Normalize one slot definition used by service-agent plugins."""
        if not isinstance(slot, dict):
            return None

        slot_id = cls._normalize_identifier(slot.get("id")) or cls._normalize_slug(slot.get("name"))
        if not slot_id:
            return None

        prompt = str(slot.get("prompt") or "").strip()
        if not prompt:
            prompt = f"Please share {slot_id.replace('_', ' ')}."

        label = str(slot.get("label") or slot_id.replace("_", " ").title()).strip()

        return {
            "id": slot_id,
            "label": label,
            "prompt": prompt,
            "required": bool(slot.get("required", True)),
        }

    @classmethod
    def _normalize_agent_fact_entry(cls, fact: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Normalize one fact entry used by service-agent plugins."""
        if not isinstance(fact, dict):
            return None

        text = str(fact.get("text") or "").strip()
        if not text:
            return None

        fact_id = cls._normalize_identifier(fact.get("id"))
        if not fact_id:
            fact_id = f"fact_{uuid4().hex[:10]}"

        source = str(fact.get("source") or "").strip()
        origin = cls._normalize_identifier(fact.get("origin") or "")
        if origin not in {"manual", "auto", "menu_ocr"}:
            origin = "manual" if source.startswith("manual") else "auto"

        tags_value = fact.get("tags", [])
        if isinstance(tags_value, str):
            tags_value = [part.strip() for part in tags_value.split(",")]
        if not isinstance(tags_value, list):
            tags_value = []
        normalized_tags: list[str] = []
        for tag in tags_value:
            normalized_tag = cls._normalize_slug(tag)
            if normalized_tag and normalized_tag not in normalized_tags:
                normalized_tags.append(normalized_tag)

        status = cls._normalize_identifier(fact.get("status") or "pending")
        if status not in {"pending", "approved", "rejected"}:
            status = "pending"

        created_at = str(fact.get("created_at") or datetime.now(UTC).isoformat()).strip()
        updated_at = str(fact.get("updated_at") or created_at).strip()
        approved_by = str(fact.get("approved_by") or "").strip()
        approved_at = str(fact.get("approved_at") or "").strip()
        confidence_raw = fact.get("confidence")
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(confidence, 1.0))
        evidence = str(fact.get("evidence") or "").strip()
        if status == "approved":
            if not approved_at:
                approved_at = datetime.now(UTC).isoformat()
        else:
            approved_by = ""
            approved_at = ""

        return {
            "id": fact_id,
            "text": text,
            "source": source,
            "origin": origin,
            "tags": normalized_tags,
            "status": status,
            "approved_by": approved_by or None,
            "approved_at": approved_at or None,
            "created_at": created_at,
            "updated_at": updated_at,
            "confidence": confidence,
            "evidence": evidence or None,
        }

    @classmethod
    def _normalize_agent_plugin_entry(cls, plugin: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Normalize one service-agent plugin entry."""
        if not isinstance(plugin, dict):
            return None

        plugin_id = cls._normalize_identifier(plugin.get("id"))
        if not plugin_id:
            return None

        slot_schema_value = plugin.get("slot_schema", [])
        if not isinstance(slot_schema_value, list):
            slot_schema_value = []
        normalized_slots: list[Dict[str, Any]] = []
        seen_slots: set[str] = set()
        for slot in slot_schema_value:
            normalized_slot = cls._normalize_agent_slot_entry(slot)
            if not normalized_slot:
                continue
            slot_id = normalized_slot["id"]
            if slot_id in seen_slots:
                continue
            seen_slots.add(slot_id)
            normalized_slots.append(normalized_slot)

        trigger_phrases_value = plugin.get("trigger_phrases", [])
        if not isinstance(trigger_phrases_value, list):
            trigger_phrases_value = []
        normalized_triggers: list[str] = []
        for phrase in trigger_phrases_value:
            phrase_text = str(phrase or "").strip().lower()
            if phrase_text and phrase_text not in normalized_triggers:
                normalized_triggers.append(phrase_text)

        channels_value = plugin.get("channels", [])
        if not isinstance(channels_value, list):
            channels_value = []
        normalized_channels: list[str] = []
        for channel in channels_value:
            channel_id = cls._normalize_channel_identifier(channel)
            if channel_id and channel_id not in normalized_channels:
                normalized_channels.append(channel_id)

        response_templates = plugin.get("response_templates", {})
        if not isinstance(response_templates, dict):
            response_templates = {}
        normalized_templates = {}
        for key in ("intro", "confirmation", "success", "cancelled", "fallback"):
            value = response_templates.get(key)
            if value is None:
                continue
            normalized_templates[key] = str(value).strip()

        knowledge_scope = plugin.get("knowledge_scope", {})
        if not isinstance(knowledge_scope, dict):
            knowledge_scope = {}
        keywords = knowledge_scope.get("keywords", [])
        if not isinstance(keywords, list):
            keywords = []
        normalized_keywords: list[str] = []
        for keyword in keywords:
            keyword_value = str(keyword or "").strip().lower()
            if keyword_value and keyword_value not in normalized_keywords:
                normalized_keywords.append(keyword_value)

        tool_bindings = plugin.get("tool_bindings", [])
        if not isinstance(tool_bindings, list):
            tool_bindings = []

        facts_value = plugin.get("knowledge_facts", [])
        if isinstance(facts_value, str):
            parsed_facts: list[dict[str, Any]] = []
            for line in [row.strip() for row in facts_value.splitlines() if row.strip()]:
                if "|" in line:
                    left, right = line.split("|", 1)
                    parsed_facts.append({"text": left.strip(), "source": right.strip()})
                else:
                    parsed_facts.append({"text": line})
            facts_value = parsed_facts
        if not isinstance(facts_value, list):
            facts_value = []
        normalized_facts: list[dict[str, Any]] = []
        seen_fact_ids: set[str] = set()
        for fact in facts_value:
            normalized_fact = cls._normalize_agent_fact_entry(fact)
            if not normalized_fact:
                continue
            fact_id = normalized_fact["id"]
            if fact_id in seen_fact_ids:
                continue
            seen_fact_ids.add(fact_id)
            normalized_facts.append(normalized_fact)

        metadata = plugin.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}

        service_category = cls._normalize_identifier(plugin.get("service_category") or "transactional")
        if service_category not in {"informational", "transactional", "hybrid"}:
            service_category = "transactional"

        service_id = cls._normalize_identifier(plugin.get("service_id"))
        industry = cls._normalize_identifier(plugin.get("industry") or "custom")

        name = str(plugin.get("name") or plugin_id.replace("_", " ").title()).strip()
        description = str(plugin.get("description") or "").strip()

        if not normalized_triggers:
            normalized_triggers = [
                name.lower(),
                plugin_id.replace("_", " "),
            ]
            if service_id:
                normalized_triggers.append(service_id.replace("_", " "))

        return {
            "id": plugin_id,
            "name": name,
            "industry": industry,
            "service_id": service_id or None,
            "service_category": service_category,
            "description": description,
            "trigger_phrases": normalized_triggers,
            "slot_schema": normalized_slots,
            "confirmation_required": bool(plugin.get("confirmation_required", True)),
            "channels": normalized_channels or ["web", "whatsapp"],
            "is_active": bool(plugin.get("is_active", True)),
            "response_templates": normalized_templates,
            "knowledge_scope": {"keywords": normalized_keywords},
            "knowledge_facts": normalized_facts,
            "strict_facts_only": bool(plugin.get("strict_facts_only", True)),
            "tool_bindings": tool_bindings,
            "metadata": metadata,
            "version": int(plugin.get("version") or 1),
        }

    @classmethod
    def _normalize_service_kb_menu_document(
        cls,
        document: Dict[str, Any],
        index: int = 0,
    ) -> Optional[Dict[str, Any]]:
        """Normalize one OCR menu document artifact stored under service_kb."""
        if not isinstance(document, dict):
            return None

        doc_id = cls._normalize_identifier(document.get("id"))
        if not doc_id:
            doc_id = f"menu_doc_{index + 1}_{uuid4().hex[:8]}"

        summary = document.get("summary", {})
        if not isinstance(summary, dict):
            summary = {}

        trace = document.get("trace", {})
        if not isinstance(trace, dict):
            trace = {}

        ocr_raw_output = document.get("ocr_raw_output", {})
        if not isinstance(ocr_raw_output, dict):
            ocr_raw_output = {}

        fact_lines_value = document.get("fact_lines", [])
        if not isinstance(fact_lines_value, list):
            fact_lines_value = []
        fact_lines: list[str] = []
        seen_fact_lines: set[str] = set()
        for line in fact_lines_value:
            text = re.sub(r"\s+", " ", str(line or "")).strip()
            if not text:
                continue
            dedupe_key = text.lower()
            if dedupe_key in seen_fact_lines:
                continue
            seen_fact_lines.add(dedupe_key)
            fact_lines.append(text)

        menu_name = str(
            document.get("menu_name")
            or summary.get("menu_name")
            or ""
        ).strip()
        source_file = str(
            document.get("source_file")
            or document.get("file_name")
            or ""
        ).strip()

        return {
            "id": doc_id,
            "menu_name": menu_name or None,
            "source_file": source_file or None,
            "scanned_at": str(document.get("scanned_at") or datetime.now(UTC).isoformat()).strip(),
            "summary": summary,
            "trace": trace,
            "fact_lines": fact_lines,
            "ocr_raw_output": ocr_raw_output,
            "ocr_raw_output_text": str(document.get("ocr_raw_output_text") or "").strip(),
        }

    @classmethod
    def _normalize_service_kb_record(cls, record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Normalize one service KB record scoped to a service/plugin."""
        if not isinstance(record, dict):
            return None

        service_id = cls._normalize_identifier(record.get("service_id"))
        plugin_id = cls._normalize_identifier(record.get("plugin_id"))
        if not service_id and not plugin_id:
            return None

        kb_id = cls._normalize_identifier(record.get("id"))
        if not kb_id:
            kb_id = f"{service_id or plugin_id}_kb"

        facts_value = record.get("facts", [])
        if not isinstance(facts_value, list):
            facts_value = []
        normalized_facts: list[dict[str, Any]] = []
        seen_fact_ids: set[str] = set()
        for fact in facts_value:
            normalized_fact = cls._normalize_agent_fact_entry(fact)
            if not normalized_fact:
                continue
            fact_id = normalized_fact["id"]
            if fact_id in seen_fact_ids:
                continue
            seen_fact_ids.add(fact_id)
            normalized_facts.append(normalized_fact)

        menu_documents_value = record.get("menu_documents", [])
        if not isinstance(menu_documents_value, list):
            menu_documents_value = []
        normalized_menu_documents: list[dict[str, Any]] = []
        seen_menu_doc_ids: set[str] = set()
        for idx, item in enumerate(menu_documents_value):
            normalized_doc = cls._normalize_service_kb_menu_document(item, index=idx)
            if not normalized_doc:
                continue
            doc_id = cls._normalize_identifier(normalized_doc.get("id"))
            if doc_id in seen_menu_doc_ids:
                continue
            seen_menu_doc_ids.add(doc_id)
            normalized_menu_documents.append(normalized_doc)

        return {
            "id": kb_id,
            "service_id": service_id or None,
            "plugin_id": plugin_id or None,
            "strict_mode": bool(record.get("strict_mode", True)),
            "facts": normalized_facts,
            "menu_documents": normalized_menu_documents,
            "version": int(record.get("version") or 1),
            "is_active": bool(record.get("is_active", True)),
            "published_at": str(record.get("published_at") or datetime.now(UTC).isoformat()).strip(),
            "published_by": str(record.get("published_by") or "").strip() or None,
            "release_notes": str(record.get("release_notes") or "").strip(),
            "completeness": record.get("completeness", {}) if isinstance(record.get("completeness"), dict) else {},
            "extracted_knowledge": str(record.get("extracted_knowledge") or "").strip(),
            "generated_extraction_prompt": str(record.get("generated_extraction_prompt") or "").strip(),
        }

    @classmethod
    def _is_ticketing_service_entry(cls, service: Any) -> bool:
        """Detect legacy ticketing plugin rows under services."""
        if not isinstance(service, dict):
            return False
        service_id = cls._normalize_identifier(service.get("id"))
        service_type = cls._normalize_identifier(service.get("type"))
        service_name = str(service.get("name") or "").strip().lower()
        if service_id in {"ticketing_agent", "ticketing_plugin", "ticketing"}:
            return True
        if service_type == "plugin" and "ticket" in service_name:
            return True
        if bool(service.get("ticketing_plugin_enabled", False)):
            return True
        if isinstance(service.get("ticketing_cases"), list):
            return True
        return False

    def _migrate_ticketing_service_to_tool(self, config: Dict[str, Any]) -> bool:
        """
        Backward-compatible migration:
        move legacy ticketing plugin config from services[] to tools[].
        """
        services = config.get("services", [])
        tools = config.get("tools", [])
        if not isinstance(services, list) or not isinstance(tools, list):
            return False

        legacy_entries = [svc for svc in services if self._is_ticketing_service_entry(svc)]
        if not legacy_entries:
            return False

        legacy = dict(legacy_entries[-1])
        legacy_cases = self._normalize_ticketing_cases(legacy.get("ticketing_cases"))
        legacy_enabled = bool(legacy.get("is_active", True)) and bool(legacy.get("ticketing_plugin_enabled", True))

        existing_tool: Dict[str, Any] | None = None
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            tool_id = self._normalize_identifier(tool.get("id"))
            handler = self._normalize_identifier(tool.get("handler"))
            if tool_id in {"ticketing", "ticketing_plugin", "ticketing_agent"}:
                existing_tool = dict(tool)
                break
            if handler == "ticket_create":
                existing_tool = dict(tool)
                break
            if isinstance(tool.get("ticketing_cases"), list):
                existing_tool = dict(tool)
                break

        business_channels = config.get("business", {}).get("channels", {})
        default_channels = []
        if isinstance(business_channels, dict):
            for channel in ("web_widget", "whatsapp"):
                if bool(business_channels.get(channel, False)):
                    default_channels.append(channel)
        if not default_channels:
            default_channels = ["web_widget", "whatsapp"]

        canonical_tool = {
            "id": "ticketing",
            "name": str((existing_tool or {}).get("name") or legacy.get("name") or "Ticketing").strip(),
            "description": str(
                (existing_tool or {}).get("description")
                or legacy.get("description")
                or "Create support tickets for unresolved user requests."
            ).strip(),
            "type": "workflow",
            "handler": str((existing_tool or {}).get("handler") or "ticket_create").strip() or "ticket_create",
            "channels": (existing_tool or {}).get("channels") or default_channels,
            "enabled": bool((existing_tool or {}).get("enabled", legacy_enabled)),
            "requires_confirmation": bool((existing_tool or {}).get("requires_confirmation", False)),
            "ticketing_plugin_enabled": bool(
                (existing_tool or {}).get("ticketing_plugin_enabled", legacy_enabled)
            ),
        }
        merged_cases = self._normalize_ticketing_cases((existing_tool or {}).get("ticketing_cases"))
        if not merged_cases:
            merged_cases = legacy_cases
        if not merged_cases:
            merged_cases = list(_DEFAULT_TICKETING_CASES)
        canonical_tool["ticketing_cases"] = merged_cases

        normalized_tool = self._normalize_tool_entry(canonical_tool)
        if not normalized_tool:
            return False

        next_tools: list[dict[str, Any]] = []
        replaced = False
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            tool_id = self._normalize_identifier(tool.get("id"))
            handler = self._normalize_identifier(tool.get("handler"))
            is_ticketing_tool = (
                tool_id in {"ticketing", "ticketing_plugin", "ticketing_agent"}
                or handler == "ticket_create"
                or isinstance(tool.get("ticketing_cases"), list)
            )
            if is_ticketing_tool:
                if not replaced:
                    next_tools.append(normalized_tool)
                    replaced = True
                continue
            normalized = self._normalize_tool_entry(tool)
            if normalized:
                next_tools.append(normalized)
        if not replaced:
            next_tools.append(normalized_tool)

        next_services: list[dict[str, Any]] = []
        for service in services:
            if self._is_ticketing_service_entry(service):
                continue
            normalized = self._normalize_service_entry(service)
            if normalized:
                next_services.append(normalized)

        config["tools"] = next_tools
        config["services"] = next_services
        return True

    @staticmethod
    def _tokenize_text(text: str) -> set[str]:
        """Tokenize user/query text for lightweight FAQ matching."""
        return {token for token in re.findall(r"[a-z0-9]+", str(text).lower()) if len(token) > 1}

    @staticmethod
    def _expand_token_forms(tokens: set[str]) -> set[str]:
        """
        Expand simple singular/plural variants so retrieval is less brittle
        (for example, room<->rooms, reservation<->reservations).
        """
        expanded = set(tokens)
        for token in list(tokens):
            value = str(token or "").strip().lower()
            if len(value) <= 2:
                continue
            if value.endswith("ies") and len(value) > 4:
                expanded.add(f"{value[:-3]}y")
            if value.endswith("s") and len(value) > 3:
                expanded.add(value[:-1])
            else:
                expanded.add(f"{value}s")
        return {token for token in expanded if token}

    @staticmethod
    def _weighted_overlap(
        query_tokens: set[str],
        candidate_tokens: set[str],
        *,
        token_weights: dict[str, float],
        total_query_weight: float,
    ) -> float:
        if not query_tokens or not candidate_tokens:
            return 0.0
        intersection = query_tokens & candidate_tokens
        if not intersection:
            return 0.0
        if total_query_weight <= 0:
            return len(intersection) / max(1, len(query_tokens))
        matched_weight = sum(float(token_weights.get(token, 1.0)) for token in intersection)
        return matched_weight / total_query_weight

    def _ensure_config_shape(self, config: Dict[str, Any]) -> bool:
        """Ensure old configs are transparently upgraded to latest schema."""
        changed = self._merge_defaults(config, self._default_config())

        prompts = config.setdefault("prompts", {})
        if not isinstance(prompts.get("system_prompt", ""), str):
            prompts["system_prompt"] = str(prompts.get("system_prompt", ""))
            changed = True

        knowledge = config.setdefault("knowledge_base", {})
        knowledge.setdefault("sources", [])
        if not isinstance(knowledge.get("sources"), list):
            knowledge["sources"] = []
            changed = True
        deduped_sources = self._dedupe_knowledge_sources(knowledge.get("sources", []))
        if deduped_sources != knowledge.get("sources"):
            knowledge["sources"] = deduped_sources
            changed = True
        knowledge.setdefault("notes", "")
        nlu_policy = knowledge.setdefault("nlu_policy", {})
        for key in ("dos", "donts"):
            if not isinstance(nlu_policy.get(key), list):
                nlu_policy[key] = []
                changed = True
        if not isinstance(nlu_policy.get("capability_constraints"), dict):
            nlu_policy["capability_constraints"] = {}
            changed = True
        library_index = knowledge.setdefault("library_index", {})
        if not isinstance(library_index, dict):
            library_index = {}
            knowledge["library_index"] = library_index
            changed = True
        library_defaults: dict[str, Any] = {
            "version": "v1",
            "source_signature": "",
            "generated_at": "",
            "book_index_generator": "default_v1",
            "book_index_generated_at": "",
            "source_count": 0,
            "documents": [],
            "pages": [],
            "books": [],
            "coverage": {
                "total_pages": 0,
                "covered_pages": 0,
                "uncovered_pages": 0,
                "coverage_ratio": 0.0,
            },
        }
        for key, default_value in library_defaults.items():
            if key not in library_index:
                library_index[key] = copy.deepcopy(default_value)
                changed = True
        for key in ("documents", "pages", "books"):
            if not isinstance(library_index.get(key), list):
                library_index[key] = []
                changed = True
        coverage = library_index.get("coverage", {})
        if not isinstance(coverage, dict):
            coverage = {}
            library_index["coverage"] = coverage
            changed = True
        for key, default_value in library_defaults["coverage"].items():
            if key not in coverage:
                coverage[key] = default_value
                changed = True

        ui_settings = config.setdefault("ui_settings", {})
        theme = ui_settings.setdefault("theme", {})
        for key, default_color in (
            ("primary_color", "#2563eb"),
            ("accent_color", "#22c55e"),
            ("background_color", "#f8fafc"),
            ("text_color", "#1e293b"),
        ):
            if not theme.get(key):
                theme[key] = default_color
                changed = True

        channels = config.setdefault("business", {}).setdefault("channels", {})
        if "web_widget" not in channels:
            channels["web_widget"] = True
            changed = True
        if "whatsapp" not in channels:
            channels["whatsapp"] = True
            changed = True

        raw_services = config.get("services", [])
        if not isinstance(raw_services, list):
            raw_services = []
            config["services"] = raw_services
            changed = True
        normalized_services = []
        seen_service_ids: set[str] = set()
        for service in raw_services:
            normalized = self._normalize_service_entry(service)
            if not normalized:
                changed = True
                continue
            service_id = normalized["id"]
            if service_id in seen_service_ids:
                changed = True
                continue
            seen_service_ids.add(service_id)
            normalized_services.append(normalized)
        if normalized_services != raw_services:
            config["services"] = normalized_services
            changed = True

        raw_phases = config.get("journey_phases", [])
        if not isinstance(raw_phases, list):
            raw_phases = []
            config["journey_phases"] = raw_phases
            changed = True
        normalized_phases: list[dict[str, Any]] = []
        seen_phase_ids: set[str] = set()
        for idx, phase in enumerate(raw_phases, start=1):
            normalized = self._normalize_phase_entry(phase, default_order=idx)
            if not normalized:
                changed = True
                continue
            phase_id = normalized["id"]
            if phase_id in seen_phase_ids:
                changed = True
                continue
            seen_phase_ids.add(phase_id)
            normalized_phases.append(normalized)
        normalized_phases.sort(key=lambda item: (int(item.get("order", 0)), item.get("name", "")))
        if normalized_phases != raw_phases:
            config["journey_phases"] = normalized_phases
            changed = True

        raw_intents = config.get("intents", [])
        if not isinstance(raw_intents, list):
            raw_intents = []
            config["intents"] = raw_intents
            changed = True
        normalized_intents = []
        seen_intent_ids: set[str] = set()
        for intent in raw_intents:
            normalized = self._normalize_intent_entry(intent)
            if not normalized:
                changed = True
                continue
            intent_id = normalized["id"]
            if intent_id in seen_intent_ids:
                changed = True
                continue
            seen_intent_ids.add(intent_id)
            normalized_intents.append(normalized)
        if normalized_intents != raw_intents:
            config["intents"] = normalized_intents
            changed = True

        raw_faq_bank = config.get("faq_bank", [])
        if not isinstance(raw_faq_bank, list):
            raw_faq_bank = []
            config["faq_bank"] = raw_faq_bank
            changed = True
        normalized_faq_bank = []
        seen_faq_ids: set[str] = set()
        for faq in raw_faq_bank:
            normalized = self._normalize_faq_entry(faq)
            if not normalized:
                changed = True
                continue
            faq_id = normalized["id"]
            if faq_id in seen_faq_ids:
                changed = True
                continue
            seen_faq_ids.add(faq_id)
            normalized_faq_bank.append(normalized)
        if normalized_faq_bank != raw_faq_bank:
            config["faq_bank"] = normalized_faq_bank
            changed = True

        raw_tools = config.get("tools", [])
        if not isinstance(raw_tools, list):
            raw_tools = []
            config["tools"] = raw_tools
            changed = True
        normalized_tools = []
        seen_tool_ids: set[str] = set()
        for tool in raw_tools:
            normalized = self._normalize_tool_entry(tool)
            if not normalized:
                changed = True
                continue
            tool_id = normalized["id"]
            if tool_id in seen_tool_ids:
                changed = True
                continue
            seen_tool_ids.add(tool_id)
            normalized_tools.append(normalized)
        if normalized_tools != raw_tools:
            config["tools"] = normalized_tools
            changed = True

        raw_agent_plugins = config.get("agent_plugins", {})
        if not isinstance(raw_agent_plugins, dict):
            raw_agent_plugins = {}
            config["agent_plugins"] = raw_agent_plugins
            changed = True
        if "enabled" not in raw_agent_plugins:
            raw_agent_plugins["enabled"] = True
            changed = True
        if "shared_context" not in raw_agent_plugins:
            raw_agent_plugins["shared_context"] = True
            changed = True
        if "strict_mode" not in raw_agent_plugins:
            raw_agent_plugins["strict_mode"] = True
            changed = True
        if "strict_unavailable_response" not in raw_agent_plugins:
            raw_agent_plugins["strict_unavailable_response"] = (
                "I can only help with configured service-agent data right now. "
                "Please contact staff for anything outside this scope."
            )
            changed = True
        if not isinstance(raw_agent_plugins.get("enabled"), bool):
            raw_agent_plugins["enabled"] = bool(raw_agent_plugins.get("enabled"))
            changed = True
        if not isinstance(raw_agent_plugins.get("shared_context"), bool):
            raw_agent_plugins["shared_context"] = bool(raw_agent_plugins.get("shared_context"))
            changed = True
        if not isinstance(raw_agent_plugins.get("strict_mode"), bool):
            raw_agent_plugins["strict_mode"] = bool(raw_agent_plugins.get("strict_mode"))
            changed = True
        if not isinstance(raw_agent_plugins.get("strict_unavailable_response"), str):
            raw_agent_plugins["strict_unavailable_response"] = str(
                raw_agent_plugins.get("strict_unavailable_response") or ""
            )
            changed = True
        raw_plugin_list = raw_agent_plugins.get("plugins", [])
        if not isinstance(raw_plugin_list, list):
            raw_plugin_list = []
            raw_agent_plugins["plugins"] = raw_plugin_list
            changed = True
        normalized_plugin_list = []
        seen_plugin_ids: set[str] = set()
        for plugin in raw_plugin_list:
            normalized = self._normalize_agent_plugin_entry(plugin)
            if not normalized:
                changed = True
                continue
            plugin_id = normalized["id"]
            if plugin_id in seen_plugin_ids:
                changed = True
                continue
            seen_plugin_ids.add(plugin_id)
            normalized_plugin_list.append(normalized)
        if normalized_plugin_list != raw_plugin_list:
            raw_agent_plugins["plugins"] = normalized_plugin_list
            changed = True

        raw_service_kb = config.get("service_kb", {})
        if not isinstance(raw_service_kb, dict):
            raw_service_kb = {}
            config["service_kb"] = raw_service_kb
            changed = True
        compiler_cfg = raw_service_kb.get("compiler", {})
        if not isinstance(compiler_cfg, dict):
            compiler_cfg = {}
            raw_service_kb["compiler"] = compiler_cfg
            changed = True
        compiler_defaults = {
            "enabled": True,
            "max_facts_per_service": 60,
            "max_source_chars": 220000,
            "max_sources": 25,
            "version": "v1",
        }
        for key, default_value in compiler_defaults.items():
            if key not in compiler_cfg:
                compiler_cfg[key] = default_value
                changed = True
        raw_kb_records = raw_service_kb.get("records", [])
        if not isinstance(raw_kb_records, list):
            raw_kb_records = []
            raw_service_kb["records"] = raw_kb_records
            changed = True
        normalized_kb_records = []
        seen_kb_ids: set[str] = set()
        for record in raw_kb_records:
            normalized = self._normalize_service_kb_record(record)
            if not normalized:
                changed = True
                continue
            kb_id = normalized["id"]
            if kb_id in seen_kb_ids:
                changed = True
                continue
            seen_kb_ids.add(kb_id)
            normalized_kb_records.append(normalized)
        if normalized_kb_records != raw_kb_records:
            raw_service_kb["records"] = normalized_kb_records
            changed = True

        if self._migrate_ticketing_service_to_tool(config):
            changed = True

        return changed

    def load_config(self) -> Dict[str, Any]:
        """Load current business configuration.
        Uses mtime-based auto-reload so reads are efficient while still syncing
        with admin updates.
        """
        config_file = self._resolve_config_file()
        if not config_file.exists():
            # Load default hotel template
            self._config = self.load_template("hotel")
            self._ensure_config_shape(self._config)
            business = self._config.setdefault("business", {})
            scoped_business_id = self._resolve_scoped_business_id()
            current_business_id = self._normalize_identifier(business.get("id"))
            if scoped_business_id and (not current_business_id or current_business_id == "default"):
                business["id"] = scoped_business_id
            self.save_config(self._config)
            return self._config

        current_mtime = config_file.stat().st_mtime
        if (
            self._config is not None
            and self._config_file == config_file
            and self._config_mtime == current_mtime
        ):
            return self._config

        with open(config_file, "r", encoding="utf-8") as f:
            self._config = json.load(f)

        if self._ensure_config_shape(self._config):
            # Persist one-time schema upgrades for backward compatibility.
            self.save_config(self._config)
            return self._config

        self._config_mtime = current_mtime
        self._config_file = config_file

        return self._config

    def save_config(self, config: Dict[str, Any]) -> bool:
        """Save business configuration."""
        try:
            config_file = self._resolve_config_file()
            config_file.parent.mkdir(parents=True, exist_ok=True)
            with open(config_file, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            self._config = config
            self._config_file = config_file
            if config_file.exists():
                self._config_mtime = config_file.stat().st_mtime
            return True
        except Exception as e:
            try:
                print(f"Error saving config: {e}")
            except OSError:
                pass
            return False

    def load_template(self, template_name: str) -> Dict[str, Any]:
        """Load a configuration template."""
        template_file = TEMPLATES_DIR / f"{template_name}_template.json"
        if template_file.exists():
            with open(template_file, "r", encoding="utf-8") as f:
                template = json.load(f)
                self._ensure_config_shape(template)
                return template
        raise FileNotFoundError(f"Template not found: {template_name}")

    def list_templates(self) -> List[Dict[str, str]]:
        """List available templates."""
        templates = []
        for file in TEMPLATES_DIR.glob("*_template.json"):
            with open(file, "r", encoding="utf-8") as f:
                data = json.load(f)
                templates.append({
                    "id": file.stem.replace("_template", ""),
                    "name": data.get("_template", file.stem),
                    "description": data.get("_description", ""),
                })
        return templates

    def get_business_info(self) -> Dict[str, Any]:
        """Get business basic info."""
        config = self.load_config()
        return config.get("business", {})

    def _discover_known_hotel_codes(self) -> set[str]:
        """
        Discover tenant/property codes that already exist in local config and KB storage.
        Used for safe alias resolution of abbreviated admin scopes (e.g. rohl_mu -> rohl_mumbai).
        """
        codes: set[str] = set()

        def _add(raw_value: Any) -> None:
            normalized = self._normalize_identifier(raw_value)
            if normalized and normalized != "default":
                codes.add(normalized)

        try:
            business = self.get_business_info()
            if isinstance(business, dict):
                _add(business.get("id"))
        except Exception:
            pass

        properties_dir = CONFIG_DIR / "properties"
        if properties_dir.exists() and properties_dir.is_dir():
            for config_path in properties_dir.glob("*.json"):
                _add(config_path.stem)
                try:
                    payload = json.loads(config_path.read_text(encoding="utf-8"))
                    if isinstance(payload, dict):
                        business = payload.get("business", {})
                        if isinstance(business, dict):
                            _add(business.get("id"))
                except Exception:
                    continue

        uploads_root = CONFIG_DIR / "knowledge_base" / "uploads"
        if uploads_root.exists() and uploads_root.is_dir():
            for tenant_dir in uploads_root.iterdir():
                if tenant_dir.is_dir():
                    _add(tenant_dir.name)

        return codes

    @classmethod
    def _resolve_hotel_code_alias(cls, requested: str, known_codes: set[str]) -> str:
        """
        Conservative alias resolver:
        only returns an alias when there is exactly one unambiguous candidate.
        """
        normalized_requested = cls._normalize_identifier(requested)
        if not normalized_requested or not known_codes:
            return normalized_requested

        if normalized_requested in known_codes:
            return normalized_requested

        requested_compact = normalized_requested.replace("_", "")
        matches: set[str] = set()

        for known in known_codes:
            normalized_known = cls._normalize_identifier(known)
            if not normalized_known:
                continue
            known_compact = normalized_known.replace("_", "")
            if (
                normalized_known.startswith(f"{normalized_requested}_")
                or normalized_requested.startswith(f"{normalized_known}_")
                or known_compact == requested_compact
                or known_compact.startswith(requested_compact)
                or requested_compact.startswith(known_compact)
            ):
                matches.add(normalized_known)

        if len(matches) == 1:
            return next(iter(matches))
        return normalized_requested

    def resolve_hotel_code(self, requested_hotel_code: Optional[str]) -> str:
        """
        Resolve incoming hotel/session code to a canonical runtime tenant code.

        This keeps chat sessions aligned with admin + RAG tenant defaults:
        - If UI sends placeholder/default codes, use business.id from config.
        - If caller sends an explicit non-placeholder code, keep it.
        """
        requested = self._normalize_identifier(requested_hotel_code)
        placeholder_codes = {
            "",
            "default",
            "test_hotel",
            # Legacy static options from the test UI template.
            "mumbai_grand",
            "delhi_palace",
            "bangalore_inn",
        }

        business = self.get_business_info()
        business_id = self._normalize_identifier(business.get("id"))
        known_codes = self._discover_known_hotel_codes()
        if business_id and business_id != "default":
            known_codes.add(business_id)

        if requested in placeholder_codes and business_id:
            return business_id

        if requested:
            resolved_alias = self._resolve_hotel_code_alias(requested, known_codes)
            return resolved_alias or requested

        if business_id:
            return business_id

        # Fallback if business.id is not yet set.
        name_slug = self._normalize_slug(business.get("name"))
        return name_slug or "default"

    def update_business_info(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Update business basic info."""
        config = self.load_config()
        config["business"].update(updates)
        self.save_config(config)
        return config["business"]

    def get_onboarding_business(self) -> Dict[str, Any]:
        """Get onboarding business profile (extended admin fields)."""
        return self.get_business_info()

    def update_onboarding_business(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Update onboarding business profile."""
        return self.update_business_info(updates)

    def get_capabilities(self) -> Dict[str, Any]:
        """Get all capabilities."""
        config = self.load_config()
        return config.get("capabilities", {})

    def update_capability(self, capability_id: str, updates: Dict[str, Any]) -> bool:
        """Update a single capability."""
        config = self.load_config()
        if capability_id in config.get("capabilities", {}):
            config["capabilities"][capability_id].update(updates)
            return self.save_config(config)
        return False

    def get_services(self) -> List[Dict[str, Any]]:
        """Get all services."""
        config = self.load_config()
        services = config.get("services", [])
        return [dict(svc) for svc in services if isinstance(svc, dict)]

    def get_service(self, service_id: str) -> Dict[str, Any] | None:
        """Get one service by normalized id."""
        normalized_id = self._normalize_identifier(service_id)
        if not normalized_id:
            return None
        for service in self.get_services():
            if self._normalize_identifier(service.get("id")) == normalized_id:
                return dict(service)
        return None

    def get_service_prompt_pack(self, service_id: str) -> Dict[str, Any]:
        """
        Return normalized prompt pack for a service.
        Falls back to a neutral system-default pack when not manually configured.
        """
        service = self.get_service(service_id)
        if not isinstance(service, dict):
            return {}

        prompt_pack = service.get("service_prompt_pack")
        if isinstance(prompt_pack, dict):
            normalized = self._normalize_service_prompt_pack(
                prompt_pack,
                service=service,
                source=str(prompt_pack.get("source") or "manual_override"),
            )
            if self._is_valid_service_prompt_pack(normalized):
                return normalized

        generated = self._generate_service_prompt_pack(service)
        return generated if self._is_valid_service_prompt_pack(generated) else {}

    def get_journey_phases(self) -> List[Dict[str, Any]]:
        """Get configured guest-journey phases."""
        config = self.load_config()
        phases = config.get("journey_phases", [])
        if not isinstance(phases, list):
            return []
        normalized: list[dict[str, Any]] = []
        for idx, phase in enumerate(phases, start=1):
            row = self._normalize_phase_entry(phase, default_order=idx)
            if row:
                normalized.append(row)
        normalized.sort(key=lambda item: (int(item.get("order", 0)), item.get("name", "")))
        return normalized

    def update_journey_phases(self, phases: List[Dict[str, Any]]) -> bool:
        """Replace journey phase definitions in config."""
        if not isinstance(phases, list):
            return False
        normalized: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for idx, phase in enumerate(phases, start=1):
            row = self._normalize_phase_entry(phase, default_order=idx)
            if not row:
                continue
            phase_id = row["id"]
            if phase_id in seen_ids:
                continue
            seen_ids.add(phase_id)
            normalized.append(row)
        if not normalized:
            return False
        normalized.sort(key=lambda item: (int(item.get("order", 0)), item.get("name", "")))

        config = self.load_config()
        config["journey_phases"] = normalized
        return self.save_config(config)

    def get_phase_services(self, phase_id: str) -> List[Dict[str, Any]]:
        """Get services mapped to one phase."""
        normalized_phase_id = self._normalize_phase_identifier(phase_id)
        if not normalized_phase_id:
            return []
        services = self.get_services()
        return [
            dict(service)
            for service in services
            if self._normalize_phase_identifier(service.get("phase_id")) == normalized_phase_id
        ]

    def get_prebuilt_phase_services(self, phase_id: str) -> List[Dict[str, Any]]:
        """List prebuilt service templates for a phase (not necessarily installed)."""
        normalized_phase_id = self._normalize_phase_identifier(phase_id)
        templates = _PHASE_PREBUILT_SERVICES.get(normalized_phase_id, [])
        rows: List[Dict[str, Any]] = []
        for item in templates:
            seeded = dict(item)
            if "ticketing_enabled" not in seeded:
                seeded["ticketing_enabled"] = True
            normalized = self._normalize_service_entry(seeded)
            rows.append(normalized if normalized else seeded)
        return rows

    def add_service(self, service: Dict[str, Any]) -> bool:
        """Add a new service."""
        normalized = self._normalize_service_entry(
            service,
            manual_prompt_override=isinstance(service, dict) and "service_prompt_pack" in service,
        )
        if not normalized:
            return False

        config = self.load_config()
        services = config.setdefault("services", [])
        for index, existing in enumerate(services):
            if self._normalize_identifier(existing.get("id")) == normalized["id"]:
                services[index] = {**existing, **normalized}
                saved = self.save_config(config)
                if saved:
                    self._maybe_auto_compile_service_kb(service_id=normalized["id"])
                return saved
        services.append(normalized)
        saved = self.save_config(config)
        if saved:
            self._maybe_auto_compile_service_kb(service_id=normalized["id"])
        return saved

    def update_service(self, service_id: str, updates: Dict[str, Any]) -> bool:
        """Update a service."""
        normalized_id = self._normalize_identifier(service_id)
        if not normalized_id:
            return False

        config = self.load_config()
        services = config.get("services", [])
        for index, service in enumerate(services):
            if self._normalize_identifier(service.get("id")) == normalized_id:
                merged = dict(service)
                merged.update(updates)
                normalized = self._normalize_service_entry(
                    merged,
                    manual_prompt_override="service_prompt_pack" in updates,
                    preserve_manual_prompt_pack="service_prompt_pack" not in updates,
                )
                if not normalized:
                    return False
                services[index] = normalized
                saved = self.save_config(config)
                if saved:
                    self._maybe_auto_compile_service_kb(service_id=normalized["id"])
                return saved
        return False

    def _prune_agent_plugins_for_services(
        self,
        config: Dict[str, Any],
        service_ids_to_remove: set[str],
    ) -> set[str]:
        """Remove agent_plugins entries linked to deleted service IDs in-place."""
        removed_plugin_ids: set[str] = set()
        if not service_ids_to_remove:
            return removed_plugin_ids

        plugins_cfg = config.get("agent_plugins")
        if not isinstance(plugins_cfg, dict):
            return removed_plugin_ids
        plugin_rows = plugins_cfg.get("plugins")
        if not isinstance(plugin_rows, list):
            return removed_plugin_ids

        kept_plugins: list[Any] = []
        for plugin in plugin_rows:
            if not isinstance(plugin, dict):
                kept_plugins.append(plugin)
                continue
            plugin_service_id = self._normalize_identifier(plugin.get("service_id"))
            plugin_id = self._normalize_identifier(plugin.get("id"))
            if plugin_service_id and plugin_service_id in service_ids_to_remove:
                if plugin_id:
                    removed_plugin_ids.add(plugin_id)
                continue
            kept_plugins.append(plugin)

        plugins_cfg["plugins"] = kept_plugins
        return removed_plugin_ids

    def _prune_service_kb_records(
        self,
        config: Dict[str, Any],
        service_ids_to_remove: set[str],
        plugin_ids_to_remove: Optional[set[str]] = None,
    ) -> None:
        """Remove service_kb records for deleted service/plugin IDs in-place."""
        service_kb = config.get("service_kb")
        if not isinstance(service_kb, dict):
            return
        records = service_kb.get("records")
        if not isinstance(records, list):
            return
        removed_plugin_ids = plugin_ids_to_remove or set()
        kept_records: list[Any] = []
        for record in records:
            if not isinstance(record, dict):
                kept_records.append(record)
                continue
            if self._normalize_identifier(record.get("service_id")) in service_ids_to_remove:
                continue
            if self._normalize_identifier(record.get("plugin_id")) in removed_plugin_ids:
                continue
            kept_records.append(record)
        service_kb["records"] = kept_records

    def delete_service(self, service_id: str) -> bool:
        """Delete a service and its KB records."""
        normalized_id = self._normalize_identifier(service_id)
        if not normalized_id:
            return False
        config = self.load_config()
        config["services"] = [
            s
            for s in config.get("services", [])
            if self._normalize_identifier(s.get("id")) != normalized_id
        ]
        removed_plugin_ids = self._prune_agent_plugins_for_services(config, {normalized_id})
        self._prune_service_kb_records(
            config,
            {normalized_id},
            plugin_ids_to_remove=removed_plugin_ids,
        )
        saved = self.save_config(config)
        if saved:
            self._maybe_auto_compile_service_kb()
        return saved

    def clear_services(self) -> bool:
        """Delete all services and their KB records from config."""
        config = self.load_config()
        service_ids_to_remove = {
            self._normalize_identifier(s.get("id"))
            for s in config.get("services", [])
            if isinstance(s, dict) and self._normalize_identifier(s.get("id"))
        }
        config["services"] = []
        self._prune_agent_plugins_for_services(config, service_ids_to_remove)
        service_kb = config.get("service_kb")
        if isinstance(service_kb, dict):
            service_kb["records"] = []
        saved = self.save_config(config)
        if saved:
            self._maybe_auto_compile_service_kb()
        return saved

    def get_agent_plugin_settings(self) -> Dict[str, Any]:
        """Get global service-agent plugin settings."""
        config = self.load_config()
        plugins_cfg = config.get("agent_plugins", {})
        if not isinstance(plugins_cfg, dict):
            return {
                "enabled": True,
                "shared_context": True,
                "strict_mode": True,
                "strict_unavailable_response": (
                    "I can only help with configured service-agent data right now. "
                    "Please contact staff for anything outside this scope."
                ),
            }
        return {
            "enabled": bool(plugins_cfg.get("enabled", True)),
            "shared_context": bool(plugins_cfg.get("shared_context", True)),
            "strict_mode": bool(plugins_cfg.get("strict_mode", True)),
            "strict_unavailable_response": str(
                plugins_cfg.get("strict_unavailable_response")
                or "I can only help with configured service-agent data right now."
            ).strip(),
        }

    def update_agent_plugin_settings(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Update global service-agent plugin settings."""
        config = self.load_config()
        plugins_cfg = config.setdefault("agent_plugins", {})
        if not isinstance(plugins_cfg, dict):
            plugins_cfg = {}
            config["agent_plugins"] = plugins_cfg

        if "enabled" in updates:
            plugins_cfg["enabled"] = bool(updates.get("enabled"))
        if "shared_context" in updates:
            plugins_cfg["shared_context"] = bool(updates.get("shared_context"))
        if "strict_mode" in updates:
            plugins_cfg["strict_mode"] = bool(updates.get("strict_mode"))
        if "strict_unavailable_response" in updates:
            plugins_cfg["strict_unavailable_response"] = str(
                updates.get("strict_unavailable_response") or ""
            ).strip()

        self.save_config(config)
        return self.get_agent_plugin_settings()

    def get_agent_plugins(
        self,
        active_only: bool = False,
        channel: Optional[str] = None,
        industry: Optional[str] = None,
        service_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get configured service-agent plugins with optional filters."""
        config = self.load_config()
        plugins_cfg = config.get("agent_plugins", {})
        plugin_rows = []
        if isinstance(plugins_cfg, dict):
            raw_plugins = plugins_cfg.get("plugins", [])
            if isinstance(raw_plugins, list):
                plugin_rows = [dict(item) for item in raw_plugins if isinstance(item, dict)]

        if not plugin_rows:
            return []

        channel_id = self._normalize_channel_identifier(channel)
        industry_id = self._normalize_identifier(industry)
        service_id_norm = self._normalize_identifier(service_id)

        filtered: list[dict[str, Any]] = []
        for plugin in plugin_rows:
            if active_only and not bool(plugin.get("is_active", True)):
                continue

            if channel_id:
                channels = plugin.get("channels", [])
                if isinstance(channels, list):
                    normalized_channels = {self._normalize_channel_identifier(ch) for ch in channels}
                else:
                    normalized_channels = set()
                if normalized_channels and channel_id not in normalized_channels:
                    continue

            if industry_id:
                plugin_industry = self._normalize_identifier(plugin.get("industry"))
                if plugin_industry and plugin_industry not in {industry_id, "custom"}:
                    continue

            if service_id_norm and self._normalize_identifier(plugin.get("service_id")) != service_id_norm:
                continue

            filtered.append(plugin)

        return filtered

    def get_agent_plugin(self, plugin_id: str) -> Optional[Dict[str, Any]]:
        """Get one service-agent plugin by ID."""
        normalized_id = self._normalize_identifier(plugin_id)
        if not normalized_id:
            return None

        for plugin in self.get_agent_plugins(active_only=False):
            if self._normalize_identifier(plugin.get("id")) == normalized_id:
                return plugin
        return None

    def _sync_plugin_facts_to_service_kb(self, plugin: Dict[str, Any]) -> None:
        """Best-effort sync from plugin fact list into service_kb scoped record."""
        if not isinstance(plugin, dict):
            return
        plugin_id = self._normalize_identifier(plugin.get("id"))
        service_id = self._normalize_identifier(plugin.get("service_id"))
        if not plugin_id:
            return

        existing_kb = self.get_service_kb_record(service_id=service_id, plugin_id=plugin_id, active_only=False)
        now_iso = datetime.now(UTC).isoformat()
        kb_candidate = {
            "id": (existing_kb or {}).get("id") or f"{service_id or plugin_id}_kb",
            "service_id": service_id or None,
            "plugin_id": plugin_id,
            "strict_mode": bool(plugin.get("strict_facts_only", True)),
            "facts": plugin.get("knowledge_facts", []),
            "menu_documents": (existing_kb or {}).get("menu_documents", []),
            "version": int((existing_kb or {}).get("version") or plugin.get("version") or 1),
            "is_active": bool((existing_kb or {}).get("is_active", True)),
            "published_at": str((existing_kb or {}).get("published_at") or now_iso),
            "published_by": (existing_kb or {}).get("published_by"),
            "release_notes": str((existing_kb or {}).get("release_notes") or ""),
            "completeness": (existing_kb or {}).get("completeness", {}),
        }
        self.upsert_service_kb_record(kb_candidate)

    def add_agent_plugin(self, plugin: Dict[str, Any]) -> bool:
        """Add (or upsert) a service-agent plugin."""
        normalized = self._normalize_agent_plugin_entry(plugin)
        if not normalized:
            return False

        config = self.load_config()
        plugins_cfg = config.setdefault("agent_plugins", {})
        if not isinstance(plugins_cfg, dict):
            plugins_cfg = {
                "enabled": True,
                "shared_context": True,
                "strict_mode": True,
                "strict_unavailable_response": (
                    "I can only help with configured service-agent data right now. "
                    "Please contact staff for anything outside this scope."
                ),
                "plugins": [],
            }
            config["agent_plugins"] = plugins_cfg

        plugin_list = plugins_cfg.setdefault("plugins", [])
        replaced = False
        for idx, existing in enumerate(plugin_list):
            if self._normalize_identifier(existing.get("id")) == normalized["id"]:
                merged = dict(existing)
                merged.update(normalized)
                plugin_list[idx] = self._normalize_agent_plugin_entry(merged) or merged
                replaced = True
                break
        if not replaced:
            plugin_list.append(normalized)

        if not self.save_config(config):
            return False
        stored = self.get_agent_plugin(normalized["id"])
        if stored:
            self._sync_plugin_facts_to_service_kb(stored)
        return True

    def update_agent_plugin(self, plugin_id: str, updates: Dict[str, Any]) -> bool:
        """Update one service-agent plugin."""
        normalized_id = self._normalize_identifier(plugin_id)
        if not normalized_id:
            return False

        config = self.load_config()
        plugins_cfg = config.setdefault("agent_plugins", {})
        if not isinstance(plugins_cfg, dict):
            plugins_cfg = {
                "enabled": True,
                "shared_context": True,
                "strict_mode": True,
                "strict_unavailable_response": (
                    "I can only help with configured service-agent data right now. "
                    "Please contact staff for anything outside this scope."
                ),
                "plugins": [],
            }
            config["agent_plugins"] = plugins_cfg
        plugin_list = plugins_cfg.setdefault("plugins", [])

        for idx, existing in enumerate(plugin_list):
            if self._normalize_identifier(existing.get("id")) != normalized_id:
                continue
            merged = dict(existing)
            merged.update(updates)
            merged["id"] = normalized_id
            normalized = self._normalize_agent_plugin_entry(merged)
            if not normalized:
                return False
            plugin_list[idx] = normalized
            if not self.save_config(config):
                return False
            stored = self.get_agent_plugin(normalized_id)
            if stored:
                self._sync_plugin_facts_to_service_kb(stored)
            return True
        return False

    def delete_agent_plugin(self, plugin_id: str) -> bool:
        """Delete one service-agent plugin."""
        normalized_id = self._normalize_identifier(plugin_id)
        config = self.load_config()
        plugins_cfg = config.setdefault("agent_plugins", {})
        if not isinstance(plugins_cfg, dict):
            plugins_cfg = {
                "enabled": True,
                "shared_context": True,
                "strict_mode": True,
                "strict_unavailable_response": (
                    "I can only help with configured service-agent data right now. "
                    "Please contact staff for anything outside this scope."
                ),
                "plugins": [],
            }
            config["agent_plugins"] = plugins_cfg
        plugin_list = plugins_cfg.setdefault("plugins", [])
        plugins_cfg["plugins"] = [
            item
            for item in plugin_list
            if self._normalize_identifier(item.get("id")) != normalized_id
        ]
        return self.save_config(config)

    def clear_agent_plugins(self) -> bool:
        """Delete all service-agent plugins."""
        config = self.load_config()
        plugins_cfg = config.setdefault("agent_plugins", {})
        if not isinstance(plugins_cfg, dict):
            plugins_cfg = {
                "enabled": True,
                "shared_context": True,
                "strict_mode": True,
                "strict_unavailable_response": (
                    "I can only help with configured service-agent data right now. "
                    "Please contact staff for anything outside this scope."
                ),
                "plugins": [],
            }
            config["agent_plugins"] = plugins_cfg
        plugins_cfg["plugins"] = []
        return self.save_config(config)

    def get_agent_plugin_facts(
        self,
        plugin_id: str,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get facts for one plugin, optionally filtered by status."""
        plugin = self.get_agent_plugin(plugin_id)
        if not plugin:
            return []

        facts = plugin.get("knowledge_facts", [])
        if not isinstance(facts, list):
            return []

        status_id = self._normalize_identifier(status)
        if not status_id:
            return [dict(item) for item in facts if isinstance(item, dict)]

        return [
            dict(item)
            for item in facts
            if isinstance(item, dict) and self._normalize_identifier(item.get("status")) == status_id
        ]

    def add_agent_plugin_fact(self, plugin_id: str, fact: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Add one fact entry to a plugin (defaults to pending approval)."""
        plugin = self.get_agent_plugin(plugin_id)
        if not plugin or not isinstance(fact, dict):
            return None

        now_iso = datetime.now(UTC).isoformat()
        candidate = dict(fact)
        candidate.setdefault("id", f"fact_{uuid4().hex[:10]}")
        candidate.setdefault("status", "pending")
        candidate.setdefault("created_at", now_iso)
        candidate.setdefault("updated_at", now_iso)

        normalized_fact = self._normalize_agent_fact_entry(candidate)
        if not normalized_fact:
            return None

        facts = plugin.get("knowledge_facts", [])
        if not isinstance(facts, list):
            facts = []
        existing_ids = {
            self._normalize_identifier(item.get("id"))
            for item in facts
            if isinstance(item, dict)
        }
        if self._normalize_identifier(normalized_fact.get("id")) in existing_ids:
            normalized_fact["id"] = f"{normalized_fact['id']}_{uuid4().hex[:6]}"

        updated_facts = [dict(item) for item in facts if isinstance(item, dict)] + [normalized_fact]
        if not self.update_agent_plugin(plugin_id, {"knowledge_facts": updated_facts}):
            return None

        return next(
            (
                item
                for item in self.get_agent_plugin_facts(plugin_id)
                if self._normalize_identifier(item.get("id")) == self._normalize_identifier(normalized_fact.get("id"))
            ),
            None,
        )

    def update_agent_plugin_fact(
        self,
        plugin_id: str,
        fact_id: str,
        updates: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Update one fact entry of a plugin."""
        normalized_fact_id = self._normalize_identifier(fact_id)
        if not normalized_fact_id:
            return None

        plugin = self.get_agent_plugin(plugin_id)
        if not plugin:
            return None

        facts = plugin.get("knowledge_facts", [])
        if not isinstance(facts, list):
            facts = []

        updated_facts: list[dict[str, Any]] = []
        found = False
        for item in facts:
            if not isinstance(item, dict):
                continue
            current_id = self._normalize_identifier(item.get("id"))
            if current_id != normalized_fact_id:
                updated_facts.append(dict(item))
                continue
            merged = dict(item)
            merged.update(updates or {})
            merged["id"] = normalized_fact_id
            merged["updated_at"] = datetime.now(UTC).isoformat()
            normalized = self._normalize_agent_fact_entry(merged)
            if not normalized:
                return None
            updated_facts.append(normalized)
            found = True

        if not found:
            return None
        if not self.update_agent_plugin(plugin_id, {"knowledge_facts": updated_facts}):
            return None

        return next(
            (
                item
                for item in self.get_agent_plugin_facts(plugin_id)
                if self._normalize_identifier(item.get("id")) == normalized_fact_id
            ),
            None,
        )

    def approve_agent_plugin_fact(
        self,
        plugin_id: str,
        fact_id: str,
        approved_by: str = "staff",
    ) -> Optional[Dict[str, Any]]:
        """Approve one plugin fact for runtime use."""
        now_iso = datetime.now(UTC).isoformat()
        return self.update_agent_plugin_fact(
            plugin_id,
            fact_id,
            {
                "status": "approved",
                "approved_by": str(approved_by or "staff").strip() or "staff",
                "approved_at": now_iso,
                "updated_at": now_iso,
            },
        )

    def reject_agent_plugin_fact(
        self,
        plugin_id: str,
        fact_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Reject one plugin fact so runtime will ignore it."""
        return self.update_agent_plugin_fact(
            plugin_id,
            fact_id,
            {
                "status": "rejected",
                "approved_by": "",
                "approved_at": "",
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )

    def delete_agent_plugin_fact(self, plugin_id: str, fact_id: str) -> bool:
        """Delete one fact entry from a plugin."""
        normalized_fact_id = self._normalize_identifier(fact_id)
        if not normalized_fact_id:
            return False

        plugin = self.get_agent_plugin(plugin_id)
        if not plugin:
            return False

        facts = plugin.get("knowledge_facts", [])
        if not isinstance(facts, list):
            facts = []
        updated_facts = [
            dict(item)
            for item in facts
            if isinstance(item, dict) and self._normalize_identifier(item.get("id")) != normalized_fact_id
        ]
        if len(updated_facts) == len(facts):
            return False
        return self.update_agent_plugin(plugin_id, {"knowledge_facts": updated_facts})

    def get_service_kb_records(
        self,
        service_id: Optional[str] = None,
        plugin_id: Optional[str] = None,
        active_only: bool = True,
    ) -> List[Dict[str, Any]]:
        """List service-scoped KB records with optional filters."""
        config = self.load_config()
        service_kb_cfg = config.get("service_kb", {})
        records = []
        if isinstance(service_kb_cfg, dict):
            records_value = service_kb_cfg.get("records", [])
            if isinstance(records_value, list):
                records = [dict(item) for item in records_value if isinstance(item, dict)]

        service_id_norm = self._normalize_identifier(service_id)
        plugin_id_norm = self._normalize_identifier(plugin_id)
        filtered: list[dict[str, Any]] = []
        for item in records:
            if active_only and not bool(item.get("is_active", True)):
                continue
            if service_id_norm and self._normalize_identifier(item.get("service_id")) != service_id_norm:
                continue
            if plugin_id_norm and self._normalize_identifier(item.get("plugin_id")) != plugin_id_norm:
                continue
            filtered.append(item)
        return filtered

    def get_service_kb_record(
        self,
        service_id: Optional[str] = None,
        plugin_id: Optional[str] = None,
        active_only: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Get one service KB record, preferring the highest version match."""
        candidates = self.get_service_kb_records(
            service_id=service_id,
            plugin_id=plugin_id,
            active_only=active_only,
        )
        if not candidates:
            return None
        candidates.sort(key=lambda item: int(item.get("version") or 0), reverse=True)
        return candidates[0]

    def upsert_service_kb_record(self, record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Upsert one service KB record."""
        normalized = self._normalize_service_kb_record(record)
        if not normalized:
            return None

        config = self.load_config()
        service_kb_cfg = config.setdefault("service_kb", {})
        if not isinstance(service_kb_cfg, dict):
            service_kb_cfg = {"records": []}
            config["service_kb"] = service_kb_cfg
        rows = service_kb_cfg.setdefault("records", [])
        if not isinstance(rows, list):
            rows = []
            service_kb_cfg["records"] = rows

        replaced = False
        for idx, existing in enumerate(rows):
            if not isinstance(existing, dict):
                continue
            existing_id = self._normalize_identifier(existing.get("id"))
            if existing_id == normalized["id"]:
                rows[idx] = normalized
                replaced = True
                break
            existing_service_id = self._normalize_identifier(existing.get("service_id"))
            existing_plugin_id = self._normalize_identifier(existing.get("plugin_id"))
            if (
                existing_service_id
                and existing_service_id == self._normalize_identifier(normalized.get("service_id"))
                and existing_plugin_id == self._normalize_identifier(normalized.get("plugin_id"))
            ):
                rows[idx] = normalized
                replaced = True
                break
        if not replaced:
            rows.append(normalized)

        if not self.save_config(config):
            return None
        return self.get_service_kb_record(
            service_id=normalized.get("service_id"),
            plugin_id=normalized.get("plugin_id"),
            active_only=False,
        )

    @staticmethod
    def _service_fact_text_key(fact: Any) -> str:
        if not isinstance(fact, dict):
            return ""
        text = re.sub(r"\s+", " ", str(fact.get("text") or "").strip().lower())
        return text

    @classmethod
    def _service_facts_fingerprint(cls, facts: Any) -> str:
        if not isinstance(facts, list):
            return ""
        parts: list[str] = []
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            text_key = cls._service_fact_text_key(fact)
            if not text_key:
                continue
            origin = str(fact.get("origin") or "").strip().lower()
            source = str(fact.get("source") or "").strip().lower()
            status = str(fact.get("status") or "").strip().lower()
            parts.append(f"{text_key}|{origin}|{source}|{status}")
        return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()

    def _service_kb_compiler_config(self, config: dict[str, Any]) -> dict[str, Any]:
        service_kb = config.setdefault("service_kb", {})
        if not isinstance(service_kb, dict):
            service_kb = {"records": []}
            config["service_kb"] = service_kb
        compiler = service_kb.setdefault("compiler", {})
        if not isinstance(compiler, dict):
            compiler = {}
            service_kb["compiler"] = compiler
        defaults = {
            "enabled": True,
            "max_facts_per_service": 60,
            "max_source_chars": 220000,
            "max_sources": 25,
            "version": "v1",
        }
        for key, default_value in defaults.items():
            if key not in compiler:
                compiler[key] = default_value
        return compiler

    @staticmethod
    def _knowledge_sources_signature(paths: list[Path]) -> str:
        signature_parts: list[str] = []
        for path in paths:
            try:
                stat = path.stat()
                signature_parts.append(f"{path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}")
            except Exception:
                signature_parts.append(str(path))
        raw = "|".join(signature_parts)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest() if raw else ""

    @staticmethod
    def _service_keywords(service: dict[str, Any]) -> set[str]:
        tokens: set[str] = set()
        seed_parts = [
            str(service.get("id") or "").replace("_", " "),
            str(service.get("name") or ""),
            str(service.get("description") or ""),
            str(service.get("cuisine") or ""),
            str(service.get("type") or ""),
            str(service.get("phase_id") or "").replace("_", " "),
        ]
        stopwords = {
            "a",
            "an",
            "the",
            "and",
            "or",
            "to",
            "for",
            "of",
            "in",
            "on",
            "at",
            "with",
            "by",
            "is",
            "are",
            "be",
            "service",
            "services",
            "hotel",
            "guest",
            "guests",
            "support",
            "request",
            "requests",
        }
        for part in seed_parts:
            for token in re.findall(r"[a-z0-9]+", str(part or "").lower()):
                if len(token) <= 2 or token in stopwords:
                    continue
                tokens.add(token)
                if token.endswith("s") and len(token) > 4:
                    singular = token[:-1]
                    if singular and singular not in stopwords:
                        tokens.add(singular)
        return tokens

    def _service_default_plugin_id(self, service_id: str) -> str:
        normalized_service_id = self._normalize_identifier(service_id)
        for plugin in self.get_agent_plugins(active_only=False):
            if self._normalize_identifier(plugin.get("service_id")) == normalized_service_id:
                plugin_id = self._normalize_identifier(plugin.get("id"))
                if plugin_id:
                    return plugin_id
        return f"{normalized_service_id}_agent" if normalized_service_id else ""

    @classmethod
    def _build_service_fact(
        cls,
        *,
        text: str,
        source: str,
        tags: list[str],
        origin: str,
        confidence: float,
        approved_by: str,
        evidence: str = "",
    ) -> dict[str, Any]:
        now_iso = datetime.now(UTC).isoformat()
        normalized_tags = []
        for tag in tags:
            normalized = cls._normalize_slug(tag)
            if normalized and normalized not in normalized_tags:
                normalized_tags.append(normalized)
        return {
            "id": f"fact_{uuid4().hex[:10]}",
            "text": re.sub(r"\s+", " ", str(text or "").strip()),
            "source": str(source or "").strip(),
            "origin": cls._normalize_identifier(origin or "auto") or "auto",
            "tags": normalized_tags,
            "status": "approved",
            "approved_by": str(approved_by or "system").strip() or "system",
            "approved_at": now_iso,
            "created_at": now_iso,
            "updated_at": now_iso,
            "confidence": max(0.0, min(float(confidence), 1.0)),
            "evidence": re.sub(r"\s+", " ", str(evidence or "").strip()) or None,
        }

    def _build_service_admin_facts(
        self,
        service: dict[str, Any],
        *,
        approved_by: str,
    ) -> list[dict[str, Any]]:
        service_id = self._normalize_identifier(service.get("id"))
        service_name = str(service.get("name") or service_id).strip()
        phase_id = self._normalize_phase_identifier(service.get("phase_id"))
        phase_label = phase_id.replace("_", " ").title() if phase_id else "General"
        tags = [service_id, phase_id, "admin_config", "service_kb"]
        facts: list[dict[str, Any]] = []

        facts.append(
            self._build_service_fact(
                text=f"{service_name} is configured as an active service for {phase_label} phase.",
                source="admin_config",
                tags=tags,
                origin="auto",
                confidence=0.99,
                approved_by=approved_by,
            )
        )
        description = str(service.get("description") or "").strip()
        if description:
            facts.append(
                self._build_service_fact(
                    text=f"{service_name}: {description}",
                    source="admin_config",
                    tags=tags + ["description"],
                    origin="auto",
                    confidence=0.98,
                    approved_by=approved_by,
                )
            )
        if "ticketing_enabled" in service:
            ticketing_enabled = bool(service.get("ticketing_enabled"))
            ticketing_text = (
                f"Ticketing is enabled for {service_name} in {phase_label} phase."
                if ticketing_enabled
                else f"Ticketing is disabled for {service_name} in {phase_label} phase."
            )
            facts.append(
                self._build_service_fact(
                    text=ticketing_text,
                    source="admin_config",
                    tags=tags + ["ticketing"],
                    origin="auto",
                    confidence=0.99,
                    approved_by=approved_by,
                )
            )
        ticketing_policy = str(service.get("ticketing_policy") or "").strip()
        if ticketing_policy:
            facts.append(
                self._build_service_fact(
                    text=f"Ticketing policy for {service_name}: {ticketing_policy}",
                    source="admin_config",
                    tags=tags + ["ticketing_policy"],
                    origin="auto",
                    confidence=0.99,
                    approved_by=approved_by,
                )
            )
        hours_value = service.get("hours")
        if isinstance(hours_value, dict) and hours_value:
            hours_chunks: list[str] = []
            for key, value in hours_value.items():
                label = str(key or "").strip()
                val = str(value or "").strip()
                if label and val:
                    hours_chunks.append(f"{label}: {val}")
            if hours_chunks:
                facts.append(
                    self._build_service_fact(
                        text=f"{service_name} service hours - " + "; ".join(hours_chunks),
                        source="admin_config",
                        tags=tags + ["hours"],
                        origin="auto",
                        confidence=0.96,
                        approved_by=approved_by,
                    )
                )
        delivery_zones = service.get("delivery_zones")
        if isinstance(delivery_zones, list):
            zones = [str(zone).strip() for zone in delivery_zones if str(zone).strip()]
            if zones:
                facts.append(
                    self._build_service_fact(
                        text=f"{service_name} delivery zones: {', '.join(zones)}.",
                        source="admin_config",
                        tags=tags + ["delivery"],
                        origin="auto",
                        confidence=0.95,
                        approved_by=approved_by,
                    )
                )
        return facts

    def _extract_service_facts_from_sources(
        self,
        service: dict[str, Any],
        source_texts: list[dict[str, str]],
        *,
        limit: int,
        approved_by: str,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        service_id = self._normalize_identifier(service.get("id"))
        phase_id = self._normalize_phase_identifier(service.get("phase_id"))
        keywords = self._service_keywords(service)
        aliases = [
            str(service.get("name") or "").strip().lower(),
            self._normalize_identifier(service.get("id")).replace("_", " ").strip(),
        ]
        aliases = [alias for alias in aliases if alias]
        scored_rows: list[tuple[float, str, str]] = []
        seen_texts: set[str] = set()

        useful_markers = {
            "available",
            "availability",
            "hours",
            "timing",
            "time",
            "book",
            "booking",
            "order",
            "price",
            "cost",
            "menu",
            "service",
            "included",
            "not available",
            "closed",
        }

        for source_row in source_texts:
            text = str(source_row.get("text") or "").strip()
            if not text:
                continue
            source_name = str(source_row.get("name") or "knowledge_source").strip()
            chunks = re.split(r"(?:\n+|(?<=[.!?])\s+)", text)
            for chunk in chunks:
                clean = re.sub(r"\s+", " ", str(chunk or "").strip(" -•\t\r\n"))
                if len(clean) < 30 or len(clean) > 320:
                    continue
                lower = clean.lower()
                tokens = set(re.findall(r"[a-z0-9]+", lower))
                overlap = keywords & tokens
                alias_hit = any(alias and alias in lower for alias in aliases)
                if not overlap and not alias_hit:
                    continue

                score = float(len(overlap))
                if alias_hit:
                    score += 2.5
                if any(marker in lower for marker in useful_markers):
                    score += 0.75
                if "not available" in lower or "unavailable" in lower:
                    score += 0.4

                dedupe_key = clean.lower()
                if dedupe_key in seen_texts:
                    continue
                seen_texts.add(dedupe_key)
                scored_rows.append((score, clean, source_name))

        scored_rows.sort(key=lambda row: (-row[0], -len(row[1])))
        facts: list[dict[str, Any]] = []
        for score, text, source_name in scored_rows[: max(limit * 2, limit)]:
            if len(facts) >= limit:
                break
            confidence = min(0.95, 0.45 + (score * 0.08))
            facts.append(
                self._build_service_fact(
                    text=text,
                    source=f"kb_source:{source_name}",
                    tags=[service_id, phase_id, "service_kb", "knowledge_source"],
                    origin="auto",
                    confidence=confidence,
                    approved_by=approved_by,
                )
            )
        return facts

    @classmethod
    def _manual_override_facts(cls, facts: Any) -> list[dict[str, Any]]:
        if not isinstance(facts, list):
            return []
        manual: list[dict[str, Any]] = []
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            status = str(fact.get("status") or "").strip().lower()
            if status == "rejected":
                continue
            origin = cls._normalize_identifier(fact.get("origin"))
            source = str(fact.get("source") or "").strip().lower()
            tags_value = fact.get("tags", [])
            tags = [cls._normalize_slug(tag) for tag in tags_value] if isinstance(tags_value, list) else []
            is_manual = origin == "manual" or source.startswith("manual") or "manual_override" in tags
            if not is_manual:
                continue
            manual.append(dict(fact))
        return manual

    @classmethod
    def _merge_service_facts(
        cls,
        manual_facts: list[dict[str, Any]],
        auto_facts: list[dict[str, Any]],
        *,
        max_total: int,
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for group in (manual_facts, auto_facts):
            for fact in group:
                text_key = cls._service_fact_text_key(fact)
                if not text_key or text_key in seen:
                    continue
                seen.add(text_key)
                merged.append(dict(fact))
                if len(merged) >= max_total:
                    return merged
        return merged

    def _build_service_pack_signature(
        self,
        service: dict[str, Any],
        *,
        source_signature: str,
        faq_bank: list[dict[str, Any]],
        nlu_policy: dict[str, Any],
        compiler_version: str,
    ) -> str:
        service_payload = {
            "id": self._normalize_identifier(service.get("id")),
            "name": str(service.get("name") or "").strip(),
            "type": str(service.get("type") or "").strip(),
            "description": str(service.get("description") or "").strip(),
            "cuisine": str(service.get("cuisine") or "").strip(),
            "phase_id": self._normalize_phase_identifier(service.get("phase_id")),
            "ticketing_enabled": bool(service.get("ticketing_enabled", True)),
            "ticketing_policy": str(service.get("ticketing_policy") or "").strip(),
            "hours": service.get("hours", {}),
            "delivery_zones": service.get("delivery_zones", []),
            "is_active": bool(service.get("is_active", True)),
            "service_prompt_pack": service.get("service_prompt_pack", {}),
            "service_prompt_pack_custom": bool(service.get("service_prompt_pack_custom", False)),
        }
        faq_signature_payload = []
        for faq in faq_bank[:120]:
            if not isinstance(faq, dict):
                continue
            if not bool(faq.get("enabled", True)):
                continue
            faq_signature_payload.append(
                {
                    "id": self._normalize_identifier(faq.get("id")),
                    "question": str(faq.get("question") or "").strip(),
                    "answer": str(faq.get("answer") or "").strip(),
                    "tags": faq.get("tags", []),
                }
            )
        payload = {
            "service": service_payload,
            "source_signature": source_signature,
            "faq_signature": faq_signature_payload,
            "nlu_policy": nlu_policy if isinstance(nlu_policy, dict) else {},
            "compiler_version": str(compiler_version or "v1"),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def compile_service_kb_records(
        self,
        *,
        service_id: Optional[str] = None,
        force: bool = False,
        max_facts_per_service: Optional[int] = None,
        preserve_manual: bool = True,
        published_by: str = "system",
    ) -> dict[str, Any]:
        config = self.load_config()
        compiler_cfg = self._service_kb_compiler_config(config)
        if not bool(compiler_cfg.get("enabled", True)):
            return {
                "compiled_count": 0,
                "skipped_count": 0,
                "compiled_service_ids": [],
                "skipped_service_ids": [],
                "records": [],
                "reason": "service_kb_compiler_disabled",
            }

        bounded_max_facts = int(
            max_facts_per_service
            or compiler_cfg.get("max_facts_per_service")
            or 60
        )
        bounded_max_facts = max(10, min(bounded_max_facts, 200))
        max_source_chars = int(compiler_cfg.get("max_source_chars") or 220000)
        max_source_chars = max(25000, min(max_source_chars, 600000))
        max_sources = int(compiler_cfg.get("max_sources") or 25)
        max_sources = max(1, min(max_sources, 100))
        compiler_version = str(compiler_cfg.get("version") or "v1").strip() or "v1"

        target_service_id = self._normalize_identifier(service_id)
        service_rows = [
            svc
            for svc in self.get_services()
            if isinstance(svc, dict) and bool(svc.get("is_active", True))
        ]
        if target_service_id:
            service_rows = [
                svc
                for svc in service_rows
                if self._normalize_identifier(svc.get("id")) == target_service_id
            ]
        if not service_rows:
            return {
                "compiled_count": 0,
                "skipped_count": 0,
                "compiled_service_ids": [],
                "skipped_service_ids": [],
                "records": [],
                "reason": "no_matching_services",
            }

        source_paths = self._resolve_knowledge_source_paths(max_sources=max_sources)
        source_signature = self._knowledge_sources_signature(source_paths)
        source_texts: list[dict[str, str]] = []
        for path in source_paths:
            text = self._load_knowledge_source_text(path, max_chars=max_source_chars)
            if not text:
                continue
            source_texts.append({"name": path.name, "path": str(path), "text": text})

        faq_bank = self.get_faq_bank()
        nlu_policy = self.get_nlu_policy()
        compiled_records: list[dict[str, Any]] = []
        compiled_service_ids: list[str] = []
        skipped_service_ids: list[str] = []
        now_iso = datetime.now(UTC).isoformat()
        published_by_clean = str(published_by or "system").strip() or "system"

        for service in service_rows:
            normalized_service_id = self._normalize_identifier(service.get("id"))
            if not normalized_service_id:
                continue
            default_plugin_id = self._service_default_plugin_id(normalized_service_id)
            existing = self.get_service_kb_record(
                service_id=normalized_service_id,
                plugin_id=default_plugin_id or None,
                active_only=False,
            ) or self.get_service_kb_record(service_id=normalized_service_id, active_only=False)
            existing_facts = (existing or {}).get("facts", [])
            existing_completeness = (existing or {}).get("completeness", {})
            if not isinstance(existing_completeness, dict):
                existing_completeness = {}

            service_signature = self._build_service_pack_signature(
                service,
                source_signature=source_signature,
                faq_bank=faq_bank,
                nlu_policy=nlu_policy,
                compiler_version=compiler_version,
            )
            existing_signature = str(existing_completeness.get("service_signature") or "").strip()
            if (
                not force
                and existing
                and existing_signature
                and existing_signature == service_signature
            ):
                skipped_service_ids.append(normalized_service_id)
                continue

            manual_facts = self._manual_override_facts(existing_facts) if preserve_manual else []
            auto_admin_facts = self._build_service_admin_facts(service, approved_by=published_by_clean)
            remaining_slots = max(0, bounded_max_facts - len(auto_admin_facts))
            auto_source_facts = self._extract_service_facts_from_sources(
                service,
                source_texts,
                limit=remaining_slots,
                approved_by=published_by_clean,
            )
            auto_facts = auto_admin_facts + auto_source_facts
            merged_facts = self._merge_service_facts(
                manual_facts,
                auto_facts,
                max_total=max(bounded_max_facts + len(manual_facts), 20),
            )

            new_fingerprint = self._service_facts_fingerprint(merged_facts)
            existing_fingerprint = self._service_facts_fingerprint(existing_facts)
            if (
                not force
                and existing
                and new_fingerprint == existing_fingerprint
                and existing_signature == service_signature
            ):
                skipped_service_ids.append(normalized_service_id)
                continue

            previous_version = int((existing or {}).get("version") or 0)
            next_version = previous_version + 1 if previous_version > 0 else 1
            plugin_id = str((existing or {}).get("plugin_id") or default_plugin_id or "").strip() or None
            kb_record = {
                "id": str((existing or {}).get("id") or f"{normalized_service_id}_kb").strip(),
                "service_id": normalized_service_id,
                "plugin_id": plugin_id,
                "strict_mode": bool((existing or {}).get("strict_mode", True)),
                "facts": merged_facts,
                "menu_documents": (existing or {}).get("menu_documents", []),
                "version": next_version,
                "is_active": True,
                "published_at": now_iso,
                "published_by": published_by_clean,
                "release_notes": (
                    f"Auto-compiled service knowledge pack ({compiler_version}) at {now_iso}."
                ),
                "completeness": {
                    "generated_at": now_iso,
                    "compiler_version": compiler_version,
                    "service_signature": service_signature,
                    "source_signature": source_signature,
                    "source_count": len(source_texts),
                    "manual_fact_count": len(manual_facts),
                    "auto_fact_count": len(auto_facts),
                    "total_fact_count": len(merged_facts),
                },
            }
            saved = self.upsert_service_kb_record(kb_record)
            if saved:
                compiled_records.append(saved)
                compiled_service_ids.append(normalized_service_id)

        return {
            "compiled_count": len(compiled_service_ids),
            "skipped_count": len(skipped_service_ids),
            "compiled_service_ids": compiled_service_ids,
            "skipped_service_ids": skipped_service_ids,
            "records": compiled_records,
            "source_count": len(source_texts),
            "source_signature": source_signature,
            "compiler_version": compiler_version,
        }

    async def enrich_service_kb_records(
        self,
        *,
        service_id: Optional[str] = None,
        force: bool = False,
        max_facts_per_service: Optional[int] = None,
        preserve_manual: bool = True,
        published_by: str = "system",
    ) -> dict[str, Any]:
        """
        Backward-compatible async wrapper used by admin routes.

        Older code paths call `enrich_service_kb_records`; current pipeline uses
        `compile_service_kb_records` as the authoritative service-KB refresh step.
        """
        result = self.compile_service_kb_records(
            service_id=service_id,
            force=force,
            max_facts_per_service=max_facts_per_service,
            preserve_manual=preserve_manual,
            published_by=published_by,
        )
        if not isinstance(result, dict):
            result = {}
        result.setdefault("mode", "compile_compat")
        result.setdefault("service_id", self._normalize_identifier(service_id))
        return result

    # ------------------------------------------------------------------
    # LLM-based service knowledge enrichment
    # ------------------------------------------------------------------

    @staticmethod
    def _display_kb_source_name(path: Path) -> str:
        name = str(path.name or "").strip() or "kb_source"
        cleaned = re.sub(r"^[0-9a-f]{8}_", "", name, flags=re.IGNORECASE)
        return cleaned or name

    def get_full_kb_documents(
        self,
        *,
        max_sources: int = 200,
        max_source_chars: int | None = None,
    ) -> list[dict[str, str]]:
        """Return KB documents with source labels for prompt assembly."""
        source_paths = self._resolve_knowledge_source_paths(max_sources=max_sources)
        documents: list[dict[str, str]] = []
        for path in source_paths:
            text = self._load_knowledge_source_text(path, max_chars=max_source_chars)
            if not text:
                continue
            documents.append(
                {
                    "source_name": self._display_kb_source_name(path),
                    "source_path": str(path.resolve()),
                    "content": text,
                }
            )
        return documents

    def get_full_kb_text(self, max_chars: int | None = None) -> str:
        """Return combined text of all knowledge sources (for LLM context)."""
        source_paths = self._resolve_knowledge_source_paths(max_sources=25)
        parts: list[str] = []
        for path in source_paths:
            text = self._load_knowledge_source_text(path)
            if text:
                parts.append(text)
        result = "\n\n".join(parts)
        if max_chars is not None:
            result = result[:max_chars]
        return result

    def get_full_kb_text_with_sources(
        self,
        *,
        max_chars: int | None = None,
        max_sources: int = 200,
        max_source_chars: int | None = None,
    ) -> str:
        """Return combined KB text with explicit source separators."""
        documents = self.get_full_kb_documents(
            max_sources=max_sources,
            max_source_chars=max_source_chars,
        )
        parts: list[str] = []
        for document in documents:
            source_name = str(document.get("source_name") or "").strip() or "kb_source"
            content = str(document.get("content") or "").strip()
            if not content:
                continue
            parts.append(f"=== SOURCE: {source_name} ===\n{content}")
        result = "\n\n".join(parts)
        if max_chars is not None:
            result = result[:max_chars]
        return result

    async def _legacy_generate_service_extraction_prompt(
        self,
        *,
        service_name: str,
        service_description: str,
        full_kb_text: str,
    ) -> str:
        """Returns the system prompt used in the extraction step — no LLM call needed here."""
        return (
            f"You are extracting a knowledge pack for a hotel chatbot service agent.\n\n"
            f"SERVICE NAME: {service_name}\n"
            f"SERVICE DESCRIPTION: {service_description}\n\n"
            "Read the entire hotel knowledge base carefully. Understand what this service is and what "
            "a guest would ever want to know about it. Then extract ALL of that information completely "
            "and accurately — every detail, every fact, every number, every policy. "
            "Do not summarize. Do not skip anything. The agent must be able to answer every possible "
            "guest question about this service using only what you extract."
        )

    async def _legacy_extract_service_knowledge_from_kb(
        self,
        *,
        extraction_prompt: str,
        full_kb_text: str,
    ) -> str:
        """Read the full KB and extract everything needed for this service — no limits."""
        from llm.client import llm_client  # local import to avoid circular dependency

        messages = [
            {"role": "system", "content": extraction_prompt},
            {
                "role": "user",
                "content": (
                    f"KNOWLEDGE BASE:\n\n{full_kb_text}\n\n"
                    "---\n\n"
                    "Read through the entire knowledge base above. Find and extract everything related to "
                    "this service that a guest might ask about. Do not miss anything — pull it all out "
                    "fully and accurately. Preserve all numbers, names, timings, prices, policies, and "
                    "specific details exactly as they appear. Use section headers to organise the output."
                ),
            },
        ]
        result = await llm_client.chat(messages, temperature=0.1)
        return result.strip()

    def _split_extraction_chunks(
        text: str,
        *,
        chunk_chars: int = 28000,
        overlap_chars: int = 1200,
        max_chunks: int = 0,
    ) -> list[str]:
        content = str(text or "").strip()
        if not content:
            return []
        if len(content) <= chunk_chars:
            return [content]

        chunks: list[str] = []
        start = 0
        step = max(1, chunk_chars - overlap_chars)
        while start < len(content):
            if max_chunks > 0 and len(chunks) >= max_chunks:
                break
            end = min(len(content), start + chunk_chars)
            chunks.append(content[start:end])
            if end >= len(content):
                break
            start += step
        return chunks

    async def _generate_service_extraction_prompt(
        self,
        *,
        service_name: str,
        service_description: str,
        full_kb_text: str,
        existing_menu_facts: list[str] | None = None,
    ) -> str:
        """Return stable extraction instructions for service knowledge generation."""
        base = (
            f"You are building a complete knowledge pack for a hotel chatbot service agent.\n\n"
            f"SERVICE NAME: {service_name}\n"
            f"SERVICE DESCRIPTION: {service_description}\n\n"
            "YOUR TASK: Copy VERBATIM from the KB everything that is relevant to this specific service.\n"
            "First, read the service name and description to understand the service's scope "
            "(e.g. room booking, in-room dining, spa, airport transfer, lost & found, etc.).\n"
            "Then copy ALL content that falls within that scope.\n\n"
            "COPY VERBATIM RULES — these are absolute, no exceptions:\n"
            "- COPY text EXACTLY as it appears in the source. Do NOT rephrase, reword, or paraphrase even a single word.\n"
            "- Do NOT replace any word with a synonym (e.g. 'bathtub' must stay 'bathtub', not 'premium fittings').\n"
            "- Do NOT summarise, compress, or shorten any relevant content.\n"
            "- Do NOT drop any detail — every item, price, variant, condition,\n"
            "  restriction, policy clause, timing, room type, menu item, specification, or name\n"
            "  must appear exactly as written in the source.\n"
            "- If a list has 10 items, copy all 10 exactly. If a table has 20 rows, copy all 20 exactly.\n"
            "- Preserve the original structure (sections, sub-items, bullet points) exactly.\n"
            "- Do NOT invent or assume any detail not present in the source.\n\n"
            "SCOPE RULE: Only include content relevant to this specific service. Skip content that is "
            "about a completely different service or topic "
            "(e.g. skip spa details when extracting for a room booking service, skip room types when "
            "extracting for airport transfer). When in doubt, include it."
        )
        if existing_menu_facts:
            facts_block = "\n".join(f"- {f}" for f in existing_menu_facts[:300])
            base += (
                "\n\nThe following facts have already been captured from a menu upload. "
                "Do NOT re-extract or repeat any information already covered by these facts. "
                "Only extract additional knowledge from the KB that is NOT present below "
                "(e.g. service policies, booking rules, timings, descriptions, conditions):\n\n"
                f"{facts_block}"
            )
        return base

    async def _extract_service_knowledge_from_kb(
        self,
        *,
        extraction_prompt: str,
        full_kb_text: str,
        knowledge_context: str = "",
    ) -> str:
        """Extract service knowledge from bounded KB context using chunked map-reduce."""
        from llm.client import llm_client  # local import to avoid circular dependency

        kb_context = str(knowledge_context or "").strip() or str(full_kb_text or "").strip()
        if not kb_context:
            return ""

        chunks = self._split_extraction_chunks(kb_context)
        partials: list[str] = []

        for idx, chunk in enumerate(chunks, start=1):
            messages = [
                {"role": "system", "content": extraction_prompt},
                {
                    "role": "user",
                    "content": (
                        f"KB EVIDENCE CHUNK {idx}/{len(chunks)}:\n\n{chunk}\n\n"
                        "Copy VERBATIM from this chunk everything relevant to the service described above.\n"
                        "IMPORTANT: Copy text EXACTLY word-for-word as it appears — do NOT rephrase, reword, "
                        "or replace any word with a synonym. Every feature name, material, fixture, price, "
                        "and detail must be copied exactly as written (e.g. 'bathtub' stays 'bathtub').\n"
                        "Do not summarise, shorten, or omit any relevant content.\n"
                        "Only skip content that is clearly about a completely different service or topic.\n"
                        "If this chunk contains nothing relevant to the service, return exactly: NO_RELEVANT_INFO\n"
                        "Do not invent missing values."
                    ),
                },
            ]
            result = await llm_client.chat(
                messages,
                temperature=0.0,
                max_tokens=4000,
            )
            extracted = str(result or "").strip()
            if not extracted:
                continue
            if extracted.upper().startswith("NO_RELEVANT_INFO"):
                continue
            partials.append(extracted)

        if not partials:
            return ""
        if len(partials) == 1:
            return partials[0].strip()
        # Preserve full evidence coverage: avoid aggressive compression that can drop details.
        return "\n\n---\n\n".join(partials).strip()

    def set_service_kb_manual_facts(
        self,
        *,
        service_id: str,
        facts: list[str],
        plugin_id: Optional[str] = None,
        published_by: str = "admin",
    ) -> Optional[dict[str, Any]]:
        normalized_service_id = self._normalize_identifier(service_id)
        if not normalized_service_id:
            return None
        clean_plugin_id = self._normalize_identifier(plugin_id)
        existing = self.get_service_kb_record(
            service_id=normalized_service_id,
            plugin_id=clean_plugin_id or None,
            active_only=False,
        ) or self.get_service_kb_record(service_id=normalized_service_id, active_only=False)

        # Ensure there is a current auto-generated baseline if record does not exist.
        if not existing:
            self.compile_service_kb_records(
                service_id=normalized_service_id,
                force=True,
                preserve_manual=True,
                published_by="system",
            )
            existing = self.get_service_kb_record(
                service_id=normalized_service_id,
                plugin_id=clean_plugin_id or None,
                active_only=False,
            ) or self.get_service_kb_record(service_id=normalized_service_id, active_only=False)

        existing_facts = (existing or {}).get("facts", [])
        auto_facts = []
        for fact in existing_facts if isinstance(existing_facts, list) else []:
            if not isinstance(fact, dict):
                continue
            origin = self._normalize_identifier(fact.get("origin"))
            source = str(fact.get("source") or "").strip().lower()
            tags_value = fact.get("tags", [])
            tags = [self._normalize_slug(tag) for tag in tags_value] if isinstance(tags_value, list) else []
            is_manual = origin == "manual" or source.startswith("manual") or "manual_override" in tags
            if is_manual:
                continue
            auto_facts.append(dict(fact))

        manual_facts: list[dict[str, Any]] = []
        published_by_clean = str(published_by or "admin").strip() or "admin"
        for line in facts if isinstance(facts, list) else []:
            text = re.sub(r"\s+", " ", str(line or "").strip())
            if not text:
                continue
            manual_facts.append(
                self._build_service_fact(
                    text=text,
                    source="manual_override",
                    tags=[normalized_service_id, "manual_override", "service_kb"],
                    origin="manual",
                    confidence=1.0,
                    approved_by=published_by_clean,
                )
            )

        merged_facts = self._merge_service_facts(
            manual_facts,
            auto_facts,
            max_total=max(20, len(manual_facts) + len(auto_facts)),
        )

        current_version = int((existing or {}).get("version") or 0)
        next_version = current_version + 1 if current_version > 0 else 1
        now_iso = datetime.now(UTC).isoformat()
        completeness = (existing or {}).get("completeness", {})
        if not isinstance(completeness, dict):
            completeness = {}
        completeness = dict(completeness)
        completeness.update(
            {
                "updated_at": now_iso,
                "manual_fact_count": len(manual_facts),
                "auto_fact_count": len(auto_facts),
                "total_fact_count": len(merged_facts),
            }
        )
        payload = {
            "id": str((existing or {}).get("id") or f"{normalized_service_id}_kb").strip(),
            "service_id": normalized_service_id,
            "plugin_id": str((existing or {}).get("plugin_id") or clean_plugin_id or self._service_default_plugin_id(normalized_service_id)).strip() or None,
            "strict_mode": bool((existing or {}).get("strict_mode", True)),
            "facts": merged_facts,
            "menu_documents": (existing or {}).get("menu_documents", []),
            "version": next_version,
            "is_active": True,
            "published_at": now_iso,
            "published_by": published_by_clean,
            "release_notes": f"Manual override update at {now_iso}.",
            "completeness": completeness,
        }
        return self.upsert_service_kb_record(payload)

    def _maybe_auto_compile_service_kb(self, service_id: Optional[str] = None) -> None:
        config = self.load_config()
        runtime_cfg = config.get("runtime", {})
        if not isinstance(runtime_cfg, dict):
            runtime_cfg = {}
        if not bool(runtime_cfg.get("service_kb_auto_compile", True)):
            return
        try:
            self.compile_service_kb_records(
                service_id=service_id,
                force=False,
                preserve_manual=True,
                published_by="system",
            )
        except Exception:
            return

    def is_menu_runtime_enabled(self) -> bool:
        """
        Backward-compatible feature flag for deterministic catalog runtime.
        Kept disabled by default so knowledge answers are served via RAG/FAQ.
        """
        config = self.load_config()
        runtime_cfg = config.get("runtime", {})
        if isinstance(runtime_cfg, dict) and "menu_runtime_enabled" in runtime_cfg:
            return bool(runtime_cfg.get("menu_runtime_enabled", False))
        return False

    def get_faq_bank(self) -> List[Dict[str, Any]]:
        """Get all FAQ bank entries."""
        config = self.load_config()
        faq_bank = config.get("faq_bank", [])
        return [dict(entry) for entry in faq_bank if isinstance(entry, dict)]

    def add_faq_entry(self, faq: Dict[str, Any]) -> bool:
        """Add a new FAQ bank entry (or upsert by ID)."""
        normalized = self._normalize_faq_entry(faq)
        if not normalized:
            return False

        config = self.load_config()
        faq_bank = config.setdefault("faq_bank", [])
        for index, existing in enumerate(faq_bank):
            if self._normalize_identifier(existing.get("id")) == normalized["id"]:
                merged = dict(existing)
                merged.update(normalized)
                faq_bank[index] = self._normalize_faq_entry(merged) or merged
                saved = self.save_config(config)
                if saved:
                    self._maybe_auto_compile_service_kb()
                return saved
        faq_bank.append(normalized)
        saved = self.save_config(config)
        if saved:
            self._maybe_auto_compile_service_kb()
        return saved

    def update_faq_entry(self, faq_id: str, updates: Dict[str, Any]) -> bool:
        """Update a FAQ entry by ID."""
        normalized_id = self._normalize_identifier(faq_id)
        if not normalized_id:
            return False

        config = self.load_config()
        faq_bank = config.get("faq_bank", [])
        for index, faq in enumerate(faq_bank):
            if self._normalize_identifier(faq.get("id")) == normalized_id:
                merged = dict(faq)
                merged.update(updates)
                merged["id"] = normalized_id
                normalized = self._normalize_faq_entry(merged)
                if not normalized:
                    return False
                faq_bank[index] = normalized
                saved = self.save_config(config)
                if saved:
                    self._maybe_auto_compile_service_kb()
                return saved
        return False

    def delete_faq_entry(self, faq_id: str) -> bool:
        """Delete a FAQ entry by ID."""
        normalized_id = self._normalize_identifier(faq_id)
        config = self.load_config()
        config["faq_bank"] = [
            faq
            for faq in config.get("faq_bank", [])
            if self._normalize_identifier(faq.get("id")) != normalized_id
        ]
        saved = self.save_config(config)
        if saved:
            self._maybe_auto_compile_service_kb()
        return saved

    def find_faq_entry(self, user_message: str, min_score: float = 0.72) -> Optional[Dict[str, Any]]:
        """Best-effort FAQ match for deterministic admin-provided Q/A answers."""
        query = str(user_message or "").strip()
        if not query:
            return None

        query_norm = self._normalize_slug(query).replace("_", " ")
        query_tokens = self._tokenize_text(query)
        if not query_norm and not query_tokens:
            return None

        best_entry: Optional[Dict[str, Any]] = None
        best_score = 0.0

        for faq in self.get_faq_bank():
            if not faq.get("enabled", True):
                continue

            faq_question = str(faq.get("question") or "").strip()
            if not faq_question:
                continue

            question_norm = self._normalize_slug(faq_question).replace("_", " ")
            question_tokens = self._tokenize_text(faq_question)
            if not question_norm and not question_tokens:
                continue

            ratio_score = SequenceMatcher(None, query_norm, question_norm).ratio() if (query_norm and question_norm) else 0.0
            overlap_score = 0.0
            if query_tokens and question_tokens:
                overlap_score = len(query_tokens & question_tokens) / max(1, len(query_tokens))

            score = max(ratio_score, overlap_score)
            if query_norm and question_norm and (query_norm in question_norm or question_norm in query_norm):
                score = max(score, 0.95)

            if score > best_score:
                best_entry = faq
                best_score = score

        if best_entry is None or best_score < min_score:
            return None

        matched = dict(best_entry)
        matched["match_score"] = round(best_score, 4)
        return matched

    def get_tools(self) -> List[Dict[str, Any]]:
        """Get all admin-configured tools."""
        config = self.load_config()
        tools = config.get("tools", [])
        return [dict(tool) for tool in tools if isinstance(tool, dict)]

    def add_tool(self, tool: Dict[str, Any]) -> bool:
        """Add a new tool (or upsert by ID)."""
        normalized = self._normalize_tool_entry(tool)
        if not normalized:
            return False

        config = self.load_config()
        tools = config.setdefault("tools", [])
        for index, existing in enumerate(tools):
            if self._normalize_identifier(existing.get("id")) == normalized["id"]:
                merged = dict(existing)
                merged.update(normalized)
                tools[index] = self._normalize_tool_entry(merged) or merged
                return self.save_config(config)
        tools.append(normalized)
        return self.save_config(config)

    def update_tool(self, tool_id: str, updates: Dict[str, Any]) -> bool:
        """Update a tool by ID."""
        normalized_id = self._normalize_identifier(tool_id)
        if not normalized_id:
            return False

        config = self.load_config()
        tools = config.get("tools", [])
        for index, tool in enumerate(tools):
            if self._normalize_identifier(tool.get("id")) == normalized_id:
                merged = dict(tool)
                merged.update(updates)
                merged["id"] = normalized_id
                normalized = self._normalize_tool_entry(merged)
                if not normalized:
                    return False
                tools[index] = normalized
                return self.save_config(config)
        return False

    def delete_tool(self, tool_id: str) -> bool:
        """Delete a tool by ID."""
        normalized_id = self._normalize_identifier(tool_id)
        config = self.load_config()
        config["tools"] = [
            tool
            for tool in config.get("tools", [])
            if self._normalize_identifier(tool.get("id")) != normalized_id
        ]
        return self.save_config(config)

    def get_intents(self) -> List[Dict[str, Any]]:
        """Get all intents."""
        config = self.load_config()
        intents = config.get("intents", [])
        return [dict(intent) for intent in intents if isinstance(intent, dict)]

    def add_intent(self, intent: Dict[str, Any]) -> bool:
        """Add a new intent (or upsert by ID)."""
        normalized = self._normalize_intent_entry(intent)
        if not normalized:
            return False

        config = self.load_config()
        intents = config.setdefault("intents", [])
        for index, existing in enumerate(intents):
            if self._normalize_identifier(existing.get("id")) == normalized["id"]:
                merged = dict(existing)
                merged.update(normalized)
                intents[index] = self._normalize_intent_entry(merged) or merged
                return self.save_config(config)
        intents.append(normalized)
        return self.save_config(config)

    def update_intent(self, intent_id: str, enabled: Any) -> bool:
        """Update intent settings. Backward compatible with bool-enabled calls."""
        normalized_id = self._normalize_identifier(intent_id)
        if not normalized_id:
            return False

        updates = enabled if isinstance(enabled, dict) else {"enabled": bool(enabled)}
        config = self.load_config()
        intents = config.get("intents", [])
        for index, intent in enumerate(intents):
            if self._normalize_identifier(intent.get("id")) == normalized_id:
                merged = dict(intent)
                merged.update(updates)
                normalized = self._normalize_intent_entry(merged)
                if not normalized:
                    return False
                intents[index] = normalized
                return self.save_config(config)
        return False

    def delete_intent(self, intent_id: str) -> bool:
        """Delete an intent by ID."""
        normalized_id = self._normalize_identifier(intent_id)
        config = self.load_config()
        config["intents"] = [
            intent
            for intent in config.get("intents", [])
            if self._normalize_identifier(intent.get("id")) != normalized_id
        ]
        return self.save_config(config)

    def resolve_intent_to_core(self, intent_id: str) -> str:
        """
        Resolve a custom intent to its core runtime intent.
        If no explicit mapping exists, returns the original ID.
        """
        normalized_id = self._normalize_identifier(intent_id)
        if not normalized_id:
            return ""

        for intent in self.get_intents():
            if self._normalize_identifier(intent.get("id")) == normalized_id:
                mapped = self._normalize_identifier(intent.get("maps_to"))
                return mapped or normalized_id
        return normalized_id

    def get_escalation_config(self) -> Dict[str, Any]:
        """Get escalation settings."""
        config = self.load_config()
        return config.get("escalation", {})

    def update_escalation_config(self, updates: Dict[str, Any]) -> bool:
        """Update escalation settings."""
        config = self.load_config()
        config.setdefault("escalation", {}).update(updates)
        return self.save_config(config)

    def get_prompts(self) -> Dict[str, str]:
        """Get custom prompts."""
        config = self.load_config()
        return config.get("prompts", {})

    def update_prompts(self, prompts: Dict[str, str]) -> bool:
        """Update custom prompts."""
        config = self.load_config()
        config.setdefault("prompts", {}).update(prompts)
        return self.save_config(config)

    def list_prompt_templates(self) -> List[Dict[str, str]]:
        """List available admin prompt templates."""
        templates: List[Dict[str, str]] = []
        for file in PROMPT_TEMPLATES_DIR.glob("*.json"):
            with open(file, "r", encoding="utf-8") as f:
                data = json.load(f)
                templates.append(
                    {
                        "id": data.get("id", file.stem),
                        "name": data.get("name", file.stem),
                        "description": data.get("description", ""),
                    }
                )
        templates.sort(key=lambda item: item["id"])
        return templates

    def get_prompt_template(self, template_id: str) -> Dict[str, Any]:
        """Load a specific prompt template by ID."""
        template_file = PROMPT_TEMPLATES_DIR / f"{template_id}.json"
        if not template_file.exists():
            raise FileNotFoundError(f"Prompt template not found: {template_id}")
        with open(template_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def apply_prompt_template(self, template_id: str) -> Dict[str, Any]:
        """Apply system prompt template and optional NLU guidance to config."""
        template = self.get_prompt_template(template_id)
        config = self.load_config()

        prompts = config.setdefault("prompts", {})
        for key in ("system_prompt", "classifier_prompt", "response_style"):
            if template.get(key):
                prompts[key] = template[key]
        prompts["template_id"] = template.get("id", template_id)

        knowledge_base = config.setdefault("knowledge_base", {})
        nlu_policy = knowledge_base.setdefault("nlu_policy", {})
        if isinstance(template.get("nlu_dos"), list):
            nlu_policy["dos"] = template["nlu_dos"]
        if isinstance(template.get("nlu_donts"), list):
            nlu_policy["donts"] = template["nlu_donts"]

        self.save_config(config)
        return {
            "template": {
                "id": template.get("id", template_id),
                "name": template.get("name", template_id),
                "description": template.get("description", ""),
            },
            "prompts": prompts,
            "knowledge_base": knowledge_base,
        }

    def get_knowledge_config(self) -> Dict[str, Any]:
        """Get knowledge + NLU policy config."""
        config = self.load_config()
        return config.get("knowledge_base", {})

    @staticmethod
    def _knowledge_source_content_hash(path: Path) -> str:
        try:
            digest = hashlib.sha1()
            with path.open("rb") as fh:
                while True:
                    chunk = fh.read(65536)
                    if not chunk:
                        break
                    digest.update(chunk)
            return digest.hexdigest()
        except Exception:
            return ""

    def _dedupe_knowledge_sources(
        self,
        sources: list[Any],
        *,
        max_sources: int = 200,
    ) -> list[str]:
        deduped: list[str] = []
        seen_paths: set[str] = set()
        seen_hashes: set[str] = set()

        for source in sources:
            if len(deduped) >= max_sources:
                break
            source_value = str(source or "").strip()
            if not source_value:
                continue
            path = Path(source_value)
            if path.exists() and path.is_file():
                resolved = str(path.resolve())
                if resolved in seen_paths:
                    continue
                content_hash = self._knowledge_source_content_hash(path)
                if content_hash and content_hash in seen_hashes:
                    continue
                deduped.append(resolved)
                seen_paths.add(resolved)
                if content_hash:
                    seen_hashes.add(content_hash)
                continue

            marker = source_value
            if marker in seen_paths:
                continue
            deduped.append(marker)
            seen_paths.add(marker)

        return deduped

    @staticmethod
    def _extract_structured_payload(raw_text: str) -> dict[str, Any]:
        text = str(raw_text or "").strip()
        if not text:
            return {}
        try:
            payload = json.loads(text)
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}

        editable = payload.get("editable")
        if isinstance(editable, dict):
            return editable

        inner_data = payload.get("data")
        if isinstance(inner_data, str):
            try:
                inner_payload = json.loads(inner_data)
            except Exception:
                inner_payload = None
            if isinstance(inner_payload, dict):
                inner_editable = inner_payload.get("editable")
                if isinstance(inner_editable, dict):
                    return inner_editable
                return inner_payload

        reserved = {"data", "orgId", "org_id", "tenant_id", "business_type"}
        editable_like = {k: v for k, v in payload.items() if k not in reserved}
        return editable_like if isinstance(editable_like, dict) else {}

    @staticmethod
    def _render_library_value(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            rendered = json.dumps(value, ensure_ascii=False, indent=2)
        else:
            rendered = str(value)
        rendered = str(rendered).replace("\r\n", "\n").replace("\\r\\n", "\n")
        rendered = rendered.replace("\\n", "\n")
        rendered = re.sub(r"[ \t]+", " ", rendered)
        rendered = re.sub(r"\n{3,}", "\n\n", rendered)
        return rendered.strip()

    @staticmethod
    def _split_plaintext_sections(text: str, *, max_chars: int = 1800) -> list[dict[str, Any]]:
        clean_text = str(text or "").replace("\r\n", "\n")
        paragraphs = [chunk.strip() for chunk in re.split(r"\n\s*\n+", clean_text) if str(chunk).strip()]
        if not paragraphs:
            compact = re.sub(r"\s+", " ", clean_text).strip()
            if not compact:
                return []
            paragraphs = [compact]

        sections: list[dict[str, Any]] = []
        section_no = 0
        for paragraph in paragraphs:
            section_no += 1
            chunk = paragraph.strip()
            if not chunk:
                continue
            if len(chunk) <= max_chars:
                sections.append(
                    {
                        "title": f"Section {section_no}",
                        "location": f"text.section_{section_no}",
                        "text": chunk,
                    }
                )
                continue

            sentence_parts = re.split(r"(?<=[.!?])\s+", chunk)
            current = ""
            split_no = 0
            for sentence in sentence_parts:
                sentence_clean = str(sentence or "").strip()
                if not sentence_clean:
                    continue
                candidate = f"{current} {sentence_clean}".strip()
                if current and len(candidate) > max_chars:
                    split_no += 1
                    sections.append(
                        {
                            "title": f"Section {section_no}.{split_no}",
                            "location": f"text.section_{section_no}_{split_no}",
                            "text": current.strip(),
                        }
                    )
                    current = sentence_clean
                else:
                    current = candidate
            if current.strip():
                split_no += 1
                sections.append(
                    {
                        "title": f"Section {section_no}.{split_no}",
                        "location": f"text.section_{section_no}_{split_no}",
                        "text": current.strip(),
                    }
                )
        return sections

    def _infer_library_topics(self, title: str, text: str) -> list[str]:
        _ = text
        base = self._normalize_slug(title)
        return [base or "general"]

    def _library_topic_aliases(self, topic_id: str) -> list[str]:
        normalized = self._normalize_slug(topic_id)
        aliases: list[str] = []
        human = re.sub(r"\s+", " ", normalized.replace("_", " ")).strip().title()
        if human:
            aliases.append(human)
        deduped: list[str] = []
        seen: set[str] = set()
        for alias in aliases:
            key = str(alias or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(str(alias).strip())
        return deduped

    def _aggregate_books_from_pages(self, pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        page_map = {str(page.get("id") or ""): page for page in pages if isinstance(page, dict)}
        book_pages: dict[str, list[str]] = defaultdict(list)
        book_sources: dict[str, set[str]] = defaultdict(set)
        book_keywords: dict[str, set[str]] = defaultdict(set)
        book_titles: dict[str, str] = {}
        book_aliases: dict[str, set[str]] = defaultdict(set)

        for page in pages:
            if not isinstance(page, dict):
                continue
            page_id = str(page.get("id") or "").strip()
            if not page_id:
                continue
            source_name = str(page.get("source_name") or "").strip()
            title = str(page.get("title") or "").strip()
            text = str(page.get("text") or "")
            title_tokens = self._tokenize_text(title)
            body_tokens = self._tokenize_text(text[:2500])
            token_candidates = {
                token for token in (title_tokens | body_tokens) if token not in _LIBRARY_TOPIC_STOPWORDS
            }

            raw_topics = page.get("topics", [])
            if not isinstance(raw_topics, list):
                raw_topics = []
            topics: list[str] = []
            for topic in raw_topics:
                normalized = self._normalize_slug(topic)
                if not normalized or normalized in topics:
                    continue
                topics.append(normalized)

            if not topics:
                topics = [self._normalize_slug(title) or "general"]

            for idx, topic_id in enumerate(topics):
                if page_id not in book_pages[topic_id]:
                    book_pages[topic_id].append(page_id)
                if source_name:
                    book_sources[topic_id].add(source_name)
                book_keywords[topic_id].update(token_candidates)

                if idx == 0:
                    primary_topic_name = str(page.get("primary_topic_name") or "").strip()
                    if not primary_topic_name:
                        primary_topic_name = re.sub(r"\s+", " ", topic_id.replace("_", " ")).strip().title()
                    if primary_topic_name:
                        book_titles[topic_id] = primary_topic_name

                if idx < len(raw_topics):
                    raw_topic_value = str(raw_topics[idx] or "").strip()
                    if raw_topic_value:
                        book_aliases[topic_id].add(raw_topic_value)
                human_alias = re.sub(r"\s+", " ", topic_id.replace("_", " ")).strip().title()
                if human_alias:
                    book_aliases[topic_id].add(human_alias)

        books: list[dict[str, Any]] = []
        for topic_id, page_ids in book_pages.items():
            char_count = sum(int((page_map.get(page_id) or {}).get("char_count") or 0) for page_id in page_ids)
            title = str(book_titles.get(topic_id) or re.sub(r"\s+", " ", topic_id.replace("_", " ")).strip().title())
            aliases = sorted(
                {
                    str(alias).strip()
                    for alias in book_aliases.get(topic_id, set())
                    if str(alias).strip() and str(alias).strip().lower() != title.lower()
                }
            )
            books.append(
                {
                    "id": topic_id,
                    "title": title,
                    "aliases": aliases,
                    "keywords": sorted(book_keywords.get(topic_id, set())),
                    "page_ids": list(page_ids),
                    "page_count": len(page_ids),
                    "char_count": char_count,
                    "sources": sorted(book_sources.get(topic_id, set())),
                }
            )
        books.sort(key=lambda row: (str(row.get("title") or "").lower(), str(row.get("id") or "")))
        return books

    def _structured_library_source_signature(self, source_paths: list[Path]) -> str:
        parts: list[str] = []
        for path in source_paths:
            content_hash = self._knowledge_source_content_hash(path)
            if content_hash:
                parts.append(content_hash)
                continue
            try:
                stat = path.stat()
                parts.append(f"{path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}")
            except Exception:
                parts.append(str(path))
        if not parts:
            return ""
        raw = "|".join(parts)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _build_structured_kb_library(self, *, max_sources: int = 25) -> dict[str, Any]:
        source_paths = self._resolve_knowledge_source_paths(max_sources=max_sources)
        source_signature = self._structured_library_source_signature(source_paths)
        generated_at = datetime.now(UTC).isoformat()

        documents: list[dict[str, Any]] = []
        pages: list[dict[str, Any]] = []
        seen_document_hashes: set[str] = set()
        page_counter = 0

        for path in source_paths:
            try:
                raw = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            if not str(raw or "").strip():
                continue

            doc_hash = hashlib.sha1(raw.encode("utf-8")).hexdigest()
            if doc_hash in seen_document_hashes:
                continue
            seen_document_hashes.add(doc_hash)

            document_id = f"doc_{len(documents) + 1:03d}_{doc_hash[:10]}"
            source_name = str(path.name or "knowledge_source")
            documents.append(
                {
                    "id": document_id,
                    "source_name": source_name,
                    "source_path": str(path),
                    "content_hash": doc_hash,
                    "char_count": len(raw),
                }
            )

            structured = self._extract_structured_payload(raw)
            if structured:
                for key, value in structured.items():
                    title = str(key or "").strip()
                    text = self._render_library_value(value)
                    if not title or not text:
                        continue
                    page_counter += 1
                    page_id = f"page_{page_counter:05d}"
                    topics = self._infer_library_topics(title, text)
                    pages.append(
                        {
                            "id": page_id,
                            "document_id": document_id,
                            "source_name": source_name,
                            "source_path": str(path),
                            "location": f"editable.{title}",
                            "title": title,
                            "text": text,
                            "topics": topics,
                            "char_count": len(text),
                        }
                    )
                continue

            sections = self._split_plaintext_sections(raw)
            for section in sections:
                text = self._render_library_value(section.get("text"))
                if not text:
                    continue
                title = str(section.get("title") or "Section").strip()
                page_counter += 1
                page_id = f"page_{page_counter:05d}"
                topics = self._infer_library_topics(title, text)
                pages.append(
                    {
                        "id": page_id,
                        "document_id": document_id,
                        "source_name": source_name,
                        "source_path": str(path),
                        "location": str(section.get("location") or f"text.section_{page_counter}"),
                        "title": title,
                        "text": text,
                        "topics": topics,
                        "char_count": len(text),
                    }
                )

        books = self._aggregate_books_from_pages(pages)

        total_pages = len(pages)
        covered_pages = sum(1 for page in pages if page.get("topics"))
        uncovered_pages = max(0, total_pages - covered_pages)
        coverage_ratio = 0.0 if total_pages <= 0 else round(covered_pages / total_pages, 4)

        return {
            "version": "v1",
            "source_signature": source_signature,
            "generated_at": generated_at,
            "book_index_generator": "default_v1",
            "book_index_generated_at": generated_at,
            "source_count": len(documents),
            "documents": documents,
            "pages": pages,
            "books": books,
            "coverage": {
                "total_pages": total_pages,
                "covered_pages": covered_pages,
                "uncovered_pages": uncovered_pages,
                "coverage_ratio": coverage_ratio,
            },
        }

    def rebuild_structured_kb_library(
        self,
        *,
        max_sources: int = 25,
        save: bool = True,
    ) -> dict[str, Any]:
        library = self._build_structured_kb_library(max_sources=max_sources)
        if not save:
            return library

        config = self.load_config()
        knowledge = config.setdefault("knowledge_base", {})
        knowledge["library_index"] = library
        self.save_config(config)
        return library

    def get_structured_kb_library(
        self,
        *,
        rebuild_if_stale: bool = True,
        max_sources: int = 25,
    ) -> dict[str, Any]:
        config = self.load_config()
        knowledge = config.setdefault("knowledge_base", {})
        library = knowledge.get("library_index", {})
        if not isinstance(library, dict):
            library = {}

        if not rebuild_if_stale:
            return library

        source_paths = self._resolve_knowledge_source_paths(max_sources=max_sources)
        current_signature = self._structured_library_source_signature(source_paths)
        cached_signature = str(library.get("source_signature") or "").strip()
        if library and cached_signature and cached_signature == current_signature:
            return library
        return self.rebuild_structured_kb_library(max_sources=max_sources, save=True)

    @staticmethod
    def _extract_json_object(text: str) -> dict[str, Any]:
        raw = str(text or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            return parsed

        first = raw.find("{")
        last = raw.rfind("}")
        if first < 0 or last <= first:
            return {}
        candidate = raw[first : last + 1]
        try:
            parsed = json.loads(candidate)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _safe_page_preview(text: str, max_chars: int = 360) -> str:
        value = re.sub(r"\s+", " ", str(text or "").strip())
        if len(value) <= max_chars:
            return value
        return value[:max_chars].rstrip() + "..."

    async def _assign_books_to_pages_with_llm(
        self,
        pages: list[dict[str, Any]],
        *,
        batch_size: int = 24,
    ) -> dict[str, list[str]]:
        from config.settings import settings
        from llm.client import llm_client  # local import to avoid circular dependency

        assignments: dict[str, list[str]] = {}
        if not isinstance(pages, list) or not pages:
            return assignments

        cleaned_pages = [page for page in pages if isinstance(page, dict) and str(page.get("id") or "").strip()]
        if not cleaned_pages:
            return assignments
        if not bool(getattr(settings, "openai_api_key", "").strip()):
            for page in cleaned_pages:
                page_id = str(page.get("id") or "").strip()
                if not page_id:
                    continue
                fallback_title = str(page.get("title") or "").strip() or "General"
                assignments[page_id] = [fallback_title]
            return assignments

        capped_batch = max(8, min(int(batch_size or 24), 32))
        for start in range(0, len(cleaned_pages), capped_batch):
            batch = cleaned_pages[start : start + capped_batch]
            lines: list[str] = []
            for page in batch:
                page_id = str(page.get("id") or "").strip()
                title = str(page.get("title") or "").strip()
                location = str(page.get("location") or "").strip()
                source = str(page.get("source_name") or "").strip()
                preview = self._safe_page_preview(page.get("text") or "")
                lines.append(
                    f"PAGE_ID: {page_id}\nTITLE: {title}\nLOCATION: {location}\nSOURCE: {source}\nPREVIEW: {preview}"
                )

            prompt = (
                "You are a KB librarian.\n"
                "Group pages into semantic topic books.\n"
                "Do not rewrite page content, do not add facts, do not remove facts.\n"
                "For each PAGE_ID, return 1-3 concise book titles that best represent the page.\n"
                "High recall: if a page can belong to multiple topics, include all relevant topics.\n"
                "Return strict JSON only with this schema:\n"
                "{\n"
                '  "assignments": [\n'
                '    {"page_id": "page_00001", "book_titles": ["Topic A", "Topic B"]}\n'
                "  ]\n"
                "}\n"
            )
            response = await llm_client.chat(
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": "\n\n".join(lines)},
                ],
                temperature=0.0,
                max_tokens=2200,
            )

            payload = self._extract_json_object(response)
            rows = payload.get("assignments", [])
            if not isinstance(rows, list):
                rows = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                page_id = str(row.get("page_id") or "").strip()
                if not page_id:
                    continue
                raw_titles = row.get("book_titles", [])
                if not isinstance(raw_titles, list):
                    raw_titles = []
                titles: list[str] = []
                for title in raw_titles:
                    text = str(title or "").strip()
                    if not text or text in titles:
                        continue
                    titles.append(text)
                if titles:
                    assignments[page_id] = titles

        for page in cleaned_pages:
            page_id = str(page.get("id") or "").strip()
            if not page_id or page_id in assignments:
                continue
            fallback_title = str(page.get("title") or "").strip() or "General"
            assignments[page_id] = [fallback_title]
        return assignments

    async def ensure_structured_kb_llm_books(
        self,
        *,
        max_sources: int = 50,
        force: bool = False,
    ) -> dict[str, Any]:
        from config.settings import settings
        if not bool(getattr(settings, "kb_indexing_enabled", False)):
            return {}
        library = self.get_structured_kb_library(rebuild_if_stale=True, max_sources=max_sources)
        pages = library.get("pages", []) if isinstance(library, dict) else []
        books = library.get("books", []) if isinstance(library, dict) else []
        if not isinstance(pages, list) or not pages:
            return library

        generator = str(library.get("book_index_generator") or "").strip().lower()
        if not force and generator == "llm_v1" and isinstance(books, list) and books:
            return library

        assignments = await self._assign_books_to_pages_with_llm(pages)
        if not assignments:
            return library

        pages_next = copy.deepcopy(pages)
        for page in pages_next:
            if not isinstance(page, dict):
                continue
            page_id = str(page.get("id") or "").strip()
            if not page_id:
                continue
            titles = assignments.get(page_id, [])
            if not isinstance(titles, list):
                titles = []
            normalized_topics: list[str] = []
            clean_titles: list[str] = []
            for raw in titles[:3]:
                title = str(raw or "").strip()
                if not title or title in clean_titles:
                    continue
                clean_titles.append(title)
                topic_id = self._normalize_slug(title)
                if topic_id and topic_id not in normalized_topics:
                    normalized_topics.append(topic_id)
            if not normalized_topics:
                fallback_topic = self._normalize_slug(page.get("title")) or "general"
                normalized_topics = [fallback_topic]
            page["topics"] = normalized_topics
            page["primary_topic_name"] = clean_titles[0] if clean_titles else str(page.get("title") or "").strip()

        books_next = self._aggregate_books_from_pages(pages_next)
        now_iso = datetime.now(UTC).isoformat()
        total_pages = len([row for row in pages_next if isinstance(row, dict)])
        covered_pages = sum(1 for page in pages_next if isinstance(page, dict) and page.get("topics"))
        uncovered_pages = max(0, total_pages - covered_pages)
        coverage_ratio = 0.0 if total_pages <= 0 else round(covered_pages / total_pages, 4)

        updated_library = dict(library)
        updated_library["pages"] = pages_next
        updated_library["books"] = books_next
        updated_library["book_index_generator"] = "llm_v1"
        updated_library["book_index_generated_at"] = now_iso
        updated_library["coverage"] = {
            "total_pages": total_pages,
            "covered_pages": covered_pages,
            "uncovered_pages": uncovered_pages,
            "coverage_ratio": coverage_ratio,
        }

        config = self.load_config()
        knowledge = config.setdefault("knowledge_base", {})
        knowledge["library_index"] = updated_library
        self.save_config(config)
        return updated_library

    async def _select_relevant_book_ids_with_llm(
        self,
        *,
        service_name: str,
        service_description: str,
        books: list[dict[str, Any]],
        max_books: int,
    ) -> list[str]:
        from config.settings import settings
        from llm.client import llm_client  # local import to avoid circular dependency

        if not isinstance(books, list) or not books:
            return []
        if not bool(getattr(settings, "openai_api_key", "").strip()):
            return []

        catalog_lines: list[str] = []
        valid_ids: set[str] = set()
        for book in books:
            if not isinstance(book, dict):
                continue
            book_id = str(book.get("id") or "").strip()
            if not book_id:
                continue
            valid_ids.add(book_id)
            title = str(book.get("title") or "").strip()
            aliases = ", ".join(str(alias).strip() for alias in list(book.get("aliases") or [])[:4] if str(alias).strip())
            page_count = int(book.get("page_count") or 0)
            catalog_lines.append(f"{book_id} | title={title} | aliases={aliases} | pages={page_count}")

        if not catalog_lines:
            return []

        prompt = (
            "You are selecting KB topic books for service-specific extraction.\n"
            "Goal: maximize recall (do not miss relevant topics).\n"
            "Choose all book IDs potentially relevant to the service request.\n"
            "Return strict JSON only:\n"
            '{ "book_ids": ["id1", "id2"] }\n'
        )
        response = await llm_client.chat(
            messages=[
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": (
                        f"SERVICE NAME: {service_name}\n"
                        f"SERVICE DESCRIPTION: {service_description}\n\n"
                        "BOOK CATALOG:\n"
                        + "\n".join(catalog_lines)
                    ),
                },
            ],
            temperature=0.0,
            max_tokens=900,
        )

        payload = self._extract_json_object(response)
        raw_ids = payload.get("book_ids", [])
        if not isinstance(raw_ids, list):
            raw_ids = []

        selected: list[str] = []
        # Keep a wider ceiling for recall: LLM shortlist + lexical union is merged later.
        hard_cap = max(24, max_books * 3)
        for value in raw_ids:
            book_id = str(value or "").strip()
            if not book_id or book_id not in valid_ids or book_id in selected:
                continue
            selected.append(book_id)
            if len(selected) >= hard_cap:
                break
        return selected

    def query_structured_kb_books(
        self,
        query: str,
        *,
        max_books: int = 8,
        max_pages_per_book: int = 8,
    ) -> list[dict[str, Any]]:
        library = self.get_structured_kb_library(rebuild_if_stale=True)
        books = library.get("books", [])
        pages = library.get("pages", [])
        if not isinstance(books, list) or not isinstance(pages, list):
            return []

        query_text = str(query or "").strip().lower()
        query_tokens = self._tokenize_text(query_text)
        query_tokens = {token for token in query_tokens if token not in _LIBRARY_TOPIC_STOPWORDS}
        query_tokens = self._expand_token_forms(query_tokens)
        if not query_tokens:
            return []

        page_map = {str(page.get("id") or ""): page for page in pages}
        book_rows: list[dict[str, Any]] = []
        token_df: dict[str, int] = defaultdict(int)
        for book in books:
            if not isinstance(book, dict):
                continue
            keyword_tokens = self._expand_token_forms(
                self._tokenize_text(" ".join(str(token) for token in book.get("keywords", [])))
            )
            alias_tokens = self._expand_token_forms(
                self._tokenize_text(" ".join(str(alias) for alias in book.get("aliases", [])))
            )
            title_tokens = self._expand_token_forms(self._tokenize_text(str(book.get("title") or "")))
            id_tokens = self._expand_token_forms(self._tokenize_text(str(book.get("id") or "").replace("_", " ")))
            book_tokens = keyword_tokens | alias_tokens | title_tokens | id_tokens
            if not book_tokens:
                continue
            for token in book_tokens:
                token_df[token] = int(token_df.get(token, 0)) + 1
            book_rows.append(
                {
                    "book": book,
                    "keyword_tokens": keyword_tokens,
                    "alias_tokens": alias_tokens,
                    "title_tokens": title_tokens,
                    "id_tokens": id_tokens,
                    "book_tokens": book_tokens,
                }
            )
        if not book_rows:
            return []

        known_query_tokens = {token for token in query_tokens if token in token_df}
        query_tokens_for_score = known_query_tokens or query_tokens
        total_books = max(1, len(book_rows))
        token_weights: dict[str, float] = {}
        for token in query_tokens_for_score:
            doc_freq = int(token_df.get(token, 0))
            token_weights[token] = 1.0 + math.log((total_books + 1) / (doc_freq + 1))
        total_query_weight = sum(token_weights.values()) or float(len(query_tokens_for_score))

        scored_books: list[tuple[float, dict[str, Any]]] = []
        for row in book_rows:
            book = row["book"]
            overlap = self._weighted_overlap(
                query_tokens_for_score,
                row["book_tokens"],
                token_weights=token_weights,
                total_query_weight=total_query_weight,
            )
            title_overlap = self._weighted_overlap(
                query_tokens_for_score,
                row["title_tokens"],
                token_weights=token_weights,
                total_query_weight=total_query_weight,
            )
            alias_overlap = self._weighted_overlap(
                query_tokens_for_score,
                row["alias_tokens"],
                token_weights=token_weights,
                total_query_weight=total_query_weight,
            )
            id_overlap = self._weighted_overlap(
                query_tokens_for_score,
                row["id_tokens"],
                token_weights=token_weights,
                total_query_weight=total_query_weight,
            )

            title_text = str(book.get("title") or "").strip().lower()
            aliases_text = " ".join(str(alias or "").lower() for alias in book.get("aliases", []))
            phrase_bonus = 0.0
            if query_text and query_text in title_text:
                phrase_bonus += 0.4
            if query_text and query_text in aliases_text:
                phrase_bonus += 0.3

            score = overlap + (0.45 * title_overlap) + (0.3 * alias_overlap) + (0.35 * id_overlap) + phrase_bonus
            if score <= 0.0:
                continue
            scored_books.append((score, book))

        scored_books.sort(key=lambda row: (-row[0], -int(row[1].get("page_count") or 0)))
        if not scored_books:
            return []

        selected: list[dict[str, Any]] = []
        for score, book in scored_books[: max(1, max_books)]:
            page_rows: list[tuple[float, dict[str, Any]]] = []
            for page_id in book.get("page_ids", []):
                page = page_map.get(str(page_id))
                if not isinstance(page, dict):
                    continue
                title = str(page.get("title") or "")
                text = str(page.get("text") or "")
                page_tokens = self._expand_token_forms(self._tokenize_text(f"{title} {text[:2400]}"))
                page_title_tokens = self._expand_token_forms(self._tokenize_text(title))
                page_overlap = self._weighted_overlap(
                    query_tokens_for_score,
                    page_tokens,
                    token_weights=token_weights,
                    total_query_weight=total_query_weight,
                )
                title_overlap = self._weighted_overlap(
                    query_tokens_for_score,
                    page_title_tokens,
                    token_weights=token_weights,
                    total_query_weight=total_query_weight,
                )
                page_score = page_overlap + (0.35 * title_overlap)
                if query_text and query_text in text.lower():
                    page_score += 0.2
                if query_text and query_text in title.lower():
                    page_score += 0.25

                page_topics = []
                for topic in page.get("topics", []):
                    normalized_topic = self._normalize_slug(topic)
                    if normalized_topic:
                        page_topics.append(normalized_topic)
                primary_topic = page_topics[0] if page_topics else ""
                book_id = self._normalize_slug(book.get("id"))
                if book_id and primary_topic == book_id:
                    page_score += 0.25
                elif book_id and book_id in page_topics:
                    page_score += 0.05

                if page_score <= 0:
                    continue
                page_rows.append((page_score, page))

            page_rows.sort(key=lambda row: (-row[0], int(row[1].get("char_count") or 0)))
            selected_pages = [row[1] for row in page_rows[: max(1, max_pages_per_book)]]
            if not selected_pages:
                continue

            selected.append(
                {
                    "book_id": str(book.get("id") or ""),
                    "book_title": str(book.get("title") or ""),
                    "book_score": round(float(score), 4),
                    "pages": selected_pages,
                }
            )
        return selected

    def _book_hits_from_ids(
        self,
        *,
        library: dict[str, Any],
        book_ids: list[str],
        max_books: int,
        max_pages: int,
    ) -> list[dict[str, Any]]:
        books = library.get("books", [])
        pages = library.get("pages", [])
        if not isinstance(books, list) or not isinstance(pages, list):
            return []
        page_map = {str(page.get("id") or ""): page for page in pages if isinstance(page, dict)}
        book_map = {
            str(book.get("id") or ""): book
            for book in books
            if isinstance(book, dict) and str(book.get("id") or "").strip()
        }
        if not book_map:
            return []

        ordered_ids: list[str] = []
        for raw in book_ids:
            book_id = str(raw or "").strip()
            if not book_id or book_id not in book_map or book_id in ordered_ids:
                continue
            ordered_ids.append(book_id)

        if not ordered_ids:
            return []

        if max_books > 0:
            ordered_ids = ordered_ids[: max(1, max_books)]
        per_book_limit = 0
        if max_pages > 0:
            per_book_limit = max(2, max_pages // max(1, len(ordered_ids)))

        hits: list[dict[str, Any]] = []
        for book_id in ordered_ids:
            book = book_map.get(book_id) or {}
            selected_pages: list[dict[str, Any]] = []
            page_ids = list(book.get("page_ids") or [])
            if per_book_limit > 0:
                page_ids = page_ids[:per_book_limit]
            for page_id in page_ids:
                page = page_map.get(str(page_id))
                if isinstance(page, dict):
                    selected_pages.append(page)
            if not selected_pages:
                continue
            hits.append(
                {
                    "book_id": book_id,
                    "book_title": str(book.get("title") or book_id),
                    "book_score": 1.0,
                    "pages": selected_pages,
                }
            )
        return hits

    def _compose_library_context_from_hits(
        self,
        *,
        library: dict[str, Any],
        book_hits: list[dict[str, Any]],
        max_pages: int,
        max_chars: int,
    ) -> dict[str, Any]:
        if not book_hits:
            return {
                "context_text": "",
                "book_ids": [],
                "page_ids": [],
                "book_count": 0,
                "page_count": 0,
                "truncated": False,
                "source_signature": str(library.get("source_signature") or "") if isinstance(library, dict) else "",
            }

        enforce_page_limit = max_pages > 0
        enforce_char_limit = max_chars > 0
        lines: list[str] = []
        used_page_ids: list[str] = []
        used_book_ids: list[str] = []
        total_chars = 0
        truncated = False
        seen_pages: set[str] = set()

        for hit in book_hits:
            book_id = str(hit.get("book_id") or "").strip()
            book_title = str(hit.get("book_title") or book_id).strip()
            if book_id and book_id not in used_book_ids:
                used_book_ids.append(book_id)
            for page in hit.get("pages", []):
                page_id = str((page or {}).get("id") or "").strip()
                if not page_id or page_id in seen_pages:
                    continue
                seen_pages.add(page_id)
                text = str((page or {}).get("text") or "").strip()
                if not text:
                    continue
                page_title = str((page or {}).get("title") or "").strip()
                source_name = str((page or {}).get("source_name") or "").strip()
                location = str((page or {}).get("location") or "").strip()
                block = (
                    f"[BOOK:{book_title} | PAGE:{page_id} | SOURCE:{source_name} | LOCATION:{location}]\n"
                    f"{page_title}\n{text}\n"
                )
                projected = total_chars + len(block) + 1
                if enforce_char_limit and projected > max_chars:
                    truncated = True
                    break
                lines.append(block)
                total_chars = projected
                used_page_ids.append(page_id)
                if enforce_page_limit and len(used_page_ids) >= max_pages:
                    truncated = True
                    break
            if truncated:
                break

        context_text = "\n".join(lines).strip()
        return {
            "context_text": context_text,
            "book_ids": used_book_ids,
            "page_ids": used_page_ids,
            "book_count": len(used_book_ids),
            "page_count": len(used_page_ids),
            "truncated": truncated,
            "source_signature": str(library.get("source_signature") or ""),
        }

    def _service_context_queries(
        self,
        *,
        service_name: str,
        service_description: str,
    ) -> list[str]:
        """
        Build multiple high-recall queries for service KB context retrieval.
        This intentionally biases toward recall so important details are not dropped.
        """
        name = re.sub(r"\s+", " ", str(service_name or "").strip())
        description = re.sub(r"\s+", " ", str(service_description or "").strip())
        combined = re.sub(r"\s+", " ", f"{name} {description}".strip())

        raw_queries: list[str] = []
        if combined:
            raw_queries.append(combined)
        if name:
            raw_queries.append(name)
        if description:
            raw_queries.append(description)
        if combined:
            raw_queries.append(
                f"{combined} amenities inclusions exclusions policy timings charges variants features"
            )

        combined_tokens = self._expand_token_forms(
            {
                token
                for token in self._tokenize_text(combined)
                if token not in _LIBRARY_TOPIC_STOPWORDS
            }
        )
        if combined_tokens:
            token_query = " ".join(sorted(combined_tokens)[:24]).strip()
            if token_query:
                raw_queries.append(token_query)

        normalized = combined.lower()
        if any(marker in normalized for marker in ("room", "suite", "accommodation", "stay", "reservation", "booking")):
            raw_queries.append(
                "room suite room types accommodation amenities bathroom bathtub shower premium ultimate prestige reservation booking"
            )
        if any(marker in normalized for marker in ("spa", "wellness", "massage", "treatment")):
            raw_queries.append(
                "spa wellness treatments therapies massage functional massage candlelight candle treatment candle therapy foot massage"
            )
        if any(marker in normalized for marker in ("food", "dining", "menu", "restaurant", "order")):
            raw_queries.append(
                "menu dishes beverages allergen prices dining in room dining restaurant"
            )
        if any(marker in normalized for marker in ("airport", "transfer", "cab", "transport")):
            raw_queries.append(
                "airport transfer pickup drop terminal vehicle timings fares"
            )
        if any(marker in normalized for marker in ("check in", "checkout", "late check", "early check")):
            raw_queries.append(
                "checkin checkout early check in late checkout policy charges timings"
            )

        deduped: list[str] = []
        seen: set[str] = set()
        for query in raw_queries:
            normalized_query = re.sub(r"\s+", " ", str(query or "").strip().lower())
            if not normalized_query or normalized_query in seen:
                continue
            seen.add(normalized_query)
            deduped.append(normalized_query)
        return deduped

    async def build_service_library_context_llm(
        self,
        *,
        service_name: str,
        service_description: str,
        max_books: int = 12,
        max_pages: int = 0,
        max_chars: int = 0,
    ) -> dict[str, Any]:
        library = await self.ensure_structured_kb_llm_books(max_sources=50)
        books = library.get("books", []) if isinstance(library, dict) else []
        if not isinstance(books, list) or not books:
            return self.build_service_library_context(
                service_name=service_name,
                service_description=service_description,
                max_books=max_books,
                max_pages=max_pages,
                max_chars=max_chars,
            )

        selected_book_ids = await self._select_relevant_book_ids_with_llm(
            service_name=service_name,
            service_description=service_description,
            books=books,
            max_books=max(1, max_books),
        )
        query_book_ids: list[str] = []
        service_queries = self._service_context_queries(
            service_name=service_name,
            service_description=service_description,
        )
        lexical_max_books = max(12, int(max_books or 1) * 3)
        lexical_max_pages_per_book = 16 if max_pages <= 0 else max(6, max_pages // max(1, lexical_max_books))
        query_id_cap = max(32, lexical_max_books * 3)
        for query in service_queries[:12]:
            hits = self.query_structured_kb_books(
                query=query,
                max_books=lexical_max_books,
                max_pages_per_book=lexical_max_pages_per_book,
            )
            for hit in hits:
                book_id = str(hit.get("book_id") or "").strip()
                if not book_id or book_id in query_book_ids:
                    continue
                query_book_ids.append(book_id)
                if len(query_book_ids) >= query_id_cap:
                    break
            if len(query_book_ids) >= query_id_cap:
                break

        merged_book_ids: list[str] = []
        for book_id in [*selected_book_ids, *query_book_ids]:
            if not book_id or book_id in merged_book_ids:
                continue
            merged_book_ids.append(book_id)

        if not merged_book_ids:
            return self.build_service_library_context(
                service_name=service_name,
                service_description=service_description,
                max_books=max_books,
                max_pages=max_pages,
                max_chars=max_chars,
            )

        if max_books > 0:
            effective_max_books = min(
                max(len(merged_book_ids), max(1, max_books) * 3),
                max(36, max_books * 6),
            )
        else:
            effective_max_books = max(1, len(merged_book_ids))

        book_hits = self._book_hits_from_ids(
            library=library,
            book_ids=merged_book_ids,
            max_books=effective_max_books,
            max_pages=max_pages,
        )
        if not book_hits:
            return self.build_service_library_context(
                service_name=service_name,
                service_description=service_description,
                max_books=max_books,
                max_pages=max_pages,
                max_chars=max_chars,
            )
        return self._compose_library_context_from_hits(
            library=library,
            book_hits=book_hits,
            max_pages=max_pages,
            max_chars=max_chars,
        )

    def build_service_library_context(
        self,
        *,
        service_name: str,
        service_description: str,
        max_books: int = 10,
        max_pages: int = 40,
        max_chars: int = 90000,
    ) -> dict[str, Any]:
        query = re.sub(r"\s+", " ", f"{service_name} {service_description}".strip())
        effective_max_books = max(1, int(max_books or 1))
        per_book_pages = 8
        if max_pages > 0:
            per_book_pages = max(2, max_pages // max(1, effective_max_books))
        book_hits = self.query_structured_kb_books(
            query=query,
            max_books=effective_max_books,
            max_pages_per_book=per_book_pages,
        )
        library = self.get_structured_kb_library(rebuild_if_stale=True)
        if not book_hits and isinstance(library, dict):
            books = library.get("books", [])
            if isinstance(books, list) and books:
                fallback_ids = [
                    str(book.get("id") or "").strip()
                    for book in sorted(
                        [row for row in books if isinstance(row, dict)],
                        key=lambda row: int(row.get("page_count") or 0),
                        reverse=True,
                    )
                    if str(book.get("id") or "").strip()
                ][:effective_max_books]
                book_hits = self._book_hits_from_ids(
                    library=library,
                    book_ids=fallback_ids,
                    max_books=effective_max_books,
                    max_pages=max_pages,
                )

        return self._compose_library_context_from_hits(
            library=library if isinstance(library, dict) else {},
            book_hits=book_hits,
            max_pages=max_pages,
            max_chars=max_chars,
        )

    def update_knowledge_config(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Update knowledge sources and NLU policy."""
        config = self.load_config()
        knowledge = config.setdefault("knowledge_base", {})

        for key in ("sources", "notes"):
            if key in updates:
                if key == "sources":
                    sources = updates.get("sources", [])
                    if not isinstance(sources, list):
                        sources = []
                    knowledge["sources"] = self._dedupe_knowledge_sources(sources)
                else:
                    knowledge[key] = updates[key]

        if "nlu_policy" in updates and isinstance(updates["nlu_policy"], dict):
            knowledge.setdefault("nlu_policy", {}).update(updates["nlu_policy"])

        saved = self.save_config(config)
        if saved:
            if "sources" in updates:
                try:
                    self.rebuild_structured_kb_library(max_sources=50, save=True)
                except Exception:
                    pass
            self._maybe_auto_compile_service_kb()
        return knowledge

    @staticmethod
    def _normalize_delivery_zones(delivery_zones: Any) -> list[str]:
        if not isinstance(delivery_zones, list):
            return []
        normalized: list[str] = []
        for zone in delivery_zones:
            zone_text = str(zone or "").strip().lower()
            if zone_text and zone_text not in normalized:
                normalized.append(zone_text)
        return normalized

    def _service_aliases_for_conflict_scan(self, service: dict[str, Any]) -> list[str]:
        aliases: list[str] = []
        service_name = str(service.get("name") or "").strip().lower()
        service_id = str(service.get("id") or "").strip().lower().replace("_", " ")
        if service_name:
            aliases.append(service_name)
        if service_id and service_id != service_name:
            aliases.append(service_id)

        deduped: list[str] = []
        seen: set[str] = set()
        for alias in aliases:
            key = re.sub(r"\s+", " ", alias).strip()
            if key and key not in seen:
                deduped.append(key)
                seen.add(key)
        return deduped

    def _resolve_knowledge_source_paths(self, max_sources: int = 25) -> list[Path]:
        knowledge = self.get_knowledge_config()
        configured_sources = knowledge.get("sources", []) if isinstance(knowledge, dict) else []
        if not isinstance(configured_sources, list):
            configured_sources = []

        resolved: list[Path] = []
        for source in configured_sources:
            if len(resolved) >= max_sources:
                break
            if not isinstance(source, str):
                continue
            path = Path(source)
            if path.exists() and path.is_file():
                resolved.append(path.resolve())

        # Fallback: if no explicit sources are configured for this scoped property,
        # use files from this property's tenant upload directory.
        if not resolved:
            tenant_id = self._resolve_scoped_business_id() or "default"
            uploads_dir = CONFIG_DIR / "knowledge_base" / "uploads" / tenant_id
            allowed_extensions = {".txt", ".json", ".md", ".markdown", ".rst"}
            if uploads_dir.exists() and uploads_dir.is_dir():
                candidates = sorted(
                    [p for p in uploads_dir.rglob("*") if p.is_file() and p.suffix.lower() in allowed_extensions],
                    key=lambda p: p.stat().st_mtime_ns if p.exists() else 0,
                    reverse=True,
                )
                for candidate in candidates:
                    if len(resolved) >= max_sources:
                        break
                    resolved.append(candidate.resolve())

        deduped: list[Path] = []
        seen: set[str] = set()
        seen_hashes: set[str] = set()
        for path in resolved:
            marker = str(path)
            if marker in seen:
                continue
            content_hash = self._knowledge_source_content_hash(path)
            if content_hash and content_hash in seen_hashes:
                continue
            seen.add(marker)
            if content_hash:
                seen_hashes.add(content_hash)
            deduped.append(path)
        return deduped

    @classmethod
    def _extract_structured_editable_text(cls, raw: str) -> str:
        """
        Parse common uploaded JSON structures and extract business-editable text
        so conflict checks work across both raw txt and JSON knowledge files.
        """
        text = str(raw or "").strip()
        if not text:
            return ""
        editable = cls._extract_structured_payload(text)
        if editable:
            return json.dumps(editable, ensure_ascii=False)
        return text

    def _load_knowledge_source_text(self, path: Path, max_chars: int | None = None) -> str:
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""
        if not raw:
            return ""
        extracted = self._extract_structured_editable_text(raw)
        compact = re.sub(r"\s+", " ", str(extracted or "")).strip()
        if max_chars is not None and max_chars > 0:
            compact = compact[: int(max_chars)]
        return compact

    @staticmethod
    def _find_marker_evidence(
        source_text: str,
        aliases: list[str],
        markers: tuple[str, ...],
        window: int = 140,
    ) -> str:
        if not source_text or not aliases or not markers:
            return ""

        lower_text = source_text.lower()
        for alias in aliases:
            normalized_alias = re.sub(r"\s+", " ", str(alias or "").strip().lower())
            if not normalized_alias:
                continue
            start_idx = 0
            while True:
                hit = lower_text.find(normalized_alias, start_idx)
                if hit < 0:
                    break
                left = max(0, hit - window)
                right = min(len(lower_text), hit + len(normalized_alias) + window)
                snippet_lower = lower_text[left:right]
                if any(marker in snippet_lower for marker in markers):
                    snippet = source_text[left:right]
                    return re.sub(r"\s+", " ", snippet).strip()[:260]
                start_idx = hit + len(normalized_alias)
        return ""

    @staticmethod
    def _service_delivery_flags(service: dict[str, Any]) -> tuple[bool, bool]:
        delivery_zones = ConfigService._normalize_delivery_zones(service.get("delivery_zones"))
        delivers_to_room = any(zone in {"room", "room_delivery", "in_room"} for zone in delivery_zones)
        dine_in_only = "dine_in_only" in delivery_zones or (bool(delivery_zones) and not delivers_to_room)
        return delivers_to_room, dine_in_only

    def get_knowledge_conflict_report(self, max_sources: int = 25) -> Dict[str, Any]:
        """
        Detect setup-vs-KB conflicts to warn admins before runtime inconsistencies.
        """
        services = self.get_services()
        source_paths = self._resolve_knowledge_source_paths(max_sources=max_sources)

        source_texts: list[dict[str, str]] = []
        for path in source_paths:
            text = self._load_knowledge_source_text(path)
            if not text:
                continue
            source_texts.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "text": text,
                }
            )

        warnings: list[dict[str, Any]] = []
        seen_warning_keys: set[tuple[str, str, str]] = set()

        def append_warning(
            code: str,
            severity: str,
            service_id: str,
            service_name: str,
            source_name: str,
            source_path: str,
            message: str,
            evidence: str,
        ) -> None:
            key = (code, service_id, source_path)
            if key in seen_warning_keys:
                return
            seen_warning_keys.add(key)
            warnings.append(
                {
                    "code": code,
                    "severity": severity,
                    "service_id": service_id,
                    "service_name": service_name,
                    "source": source_name,
                    "source_path": source_path,
                    "message": message,
                    "evidence": evidence,
                }
            )

        for service in services:
            if not isinstance(service, dict):
                continue
            service_id = self._normalize_identifier(service.get("id"))
            if not service_id:
                continue
            service_name = str(service.get("name") or service_id).strip()
            is_active = bool(service.get("is_active", True))
            delivers_to_room, dine_in_only = self._service_delivery_flags(service)
            aliases = self._service_aliases_for_conflict_scan(service)
            if not aliases:
                continue

            for source in source_texts:
                source_name = source["name"]
                source_path = source["path"]
                source_text = source["text"]

                available_hit = self._find_marker_evidence(
                    source_text,
                    aliases,
                    _KB_CONFLICT_AVAILABLE_MARKERS,
                )
                unavailable_hit = self._find_marker_evidence(
                    source_text,
                    aliases,
                    _KB_CONFLICT_UNAVAILABLE_MARKERS,
                )
                room_delivery_hit = self._find_marker_evidence(
                    source_text,
                    aliases,
                    _KB_CONFLICT_ROOM_DELIVERY_MARKERS,
                )
                dine_in_hit = self._find_marker_evidence(
                    source_text,
                    aliases,
                    _KB_CONFLICT_DINE_IN_ONLY_MARKERS,
                )

                if not is_active and available_hit and not unavailable_hit:
                    append_warning(
                        code="inactive_service_marked_available",
                        severity="high",
                        service_id=service_id,
                        service_name=service_name,
                        source_name=source_name,
                        source_path=source_path,
                        message=(
                            f"Service '{service_name}' is inactive in setup, but KB content suggests it is available."
                        ),
                        evidence=available_hit,
                    )

                if is_active and unavailable_hit:
                    append_warning(
                        code="active_service_marked_unavailable",
                        severity="high",
                        service_id=service_id,
                        service_name=service_name,
                        source_name=source_name,
                        source_path=source_path,
                        message=(
                            f"Service '{service_name}' is active in setup, but KB content suggests it is unavailable/closed."
                        ),
                        evidence=unavailable_hit,
                    )

                if dine_in_only and room_delivery_hit:
                    append_warning(
                        code="dine_in_only_conflicts_with_kb_delivery",
                        severity="medium",
                        service_id=service_id,
                        service_name=service_name,
                        source_name=source_name,
                        source_path=source_path,
                        message=(
                            f"Service '{service_name}' is dine-in only in setup, but KB mentions room delivery."
                        ),
                        evidence=room_delivery_hit,
                    )

                if delivers_to_room and dine_in_hit:
                    append_warning(
                        code="room_delivery_conflicts_with_kb_dine_in_only",
                        severity="medium",
                        service_id=service_id,
                        service_name=service_name,
                        source_name=source_name,
                        source_path=source_path,
                        message=(
                            f"Service '{service_name}' allows room delivery in setup, but KB suggests dine-in only/no room delivery."
                        ),
                        evidence=dine_in_hit,
                    )

        warnings.sort(
            key=lambda item: (
                0 if item.get("severity") == "high" else 1,
                str(item.get("service_name") or ""),
                str(item.get("code") or ""),
            )
        )

        return {
            "checked_at": datetime.now(UTC).isoformat(),
            "services_checked": len([svc for svc in services if isinstance(svc, dict)]),
            "sources_checked": [str(path) for path in source_paths],
            "warnings": warnings,
        }

    def get_ui_settings(self) -> Dict[str, Any]:
        """Get UI customization settings for channels and widget."""
        config = self.load_config()
        return config.get("ui_settings", {})

    def update_ui_settings(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Update UI customization settings."""
        config = self.load_config()
        ui_settings = config.setdefault("ui_settings", {})

        for key in ("theme", "widget", "channels", "industry_features"):
            if key in updates:
                if isinstance(updates[key], dict):
                    ui_settings.setdefault(key, {}).update(updates[key])
                else:
                    ui_settings[key] = updates[key]

        # Keep business-level channel flags in sync.
        channels = ui_settings.get("channels", {})
        business_channels = config.setdefault("business", {}).setdefault("channels", {})
        web_cfg = channels.get("web_widget", {})
        if isinstance(web_cfg, dict):
            web_enabled = web_cfg.get("enabled")
        else:
            web_enabled = web_cfg
        whatsapp_cfg = channels.get("whatsapp", {})
        if isinstance(whatsapp_cfg, dict):
            whatsapp_enabled = whatsapp_cfg.get("enabled")
        else:
            whatsapp_enabled = whatsapp_cfg

        if isinstance(web_enabled, bool):
            business_channels["web_widget"] = web_enabled
        if isinstance(whatsapp_enabled, bool):
            business_channels["whatsapp"] = whatsapp_enabled

        self.save_config(config)
        return ui_settings

    def get_nlu_policy(self) -> Dict[str, Any]:
        """Get NLU do/don't guardrails."""
        knowledge_base = self.get_knowledge_config()
        return knowledge_base.get("nlu_policy", {})

    def get_active_system_prompt(self) -> str:
        """Get the effective admin-defined system prompt."""
        prompts = self.get_prompts()
        return prompts.get("system_prompt", "").strip()

    def apply_template(self, template_name: str, business_info: Dict[str, Any]) -> Dict[str, Any]:
        """Apply a template with custom business info."""
        template = self.load_template(template_name)
        template["business"].update(business_info)
        self.save_config(template)
        return template

    def export_config(self) -> str:
        """Export config as JSON string."""
        config = self.load_config()
        return json.dumps(config, indent=2, ensure_ascii=False)

    def import_config(self, config_json: str) -> bool:
        """Import config from JSON string."""
        try:
            config = json.loads(config_json)
            self._ensure_config_shape(config)
            return self.save_config(config)
        except json.JSONDecodeError:
            return False

    def reload_config(self) -> Dict[str, Any]:
        """Force reload config from file (clear cache)."""
        self._config = None
        self._config_mtime = None
        self._config_file = None
        return self.load_config()

    def get_capability_summary(self, hotel_code: str = None) -> Dict[str, Any]:
        """
        Get capability summary for chatbot context.
        Converts JSON config to the format expected by chat_service.
        """
        config = self.load_config()
        runtime_cfg = config.get("runtime", {})
        if not isinstance(runtime_cfg, dict):
            runtime_cfg = {}
        if bool(runtime_cfg.get("service_kb_auto_compile", True)):
            try:
                self.compile_service_kb_records(
                    force=False,
                    preserve_manual=True,
                    published_by="system",
                )
                config = self.load_config()
            except Exception:
                # Capability summary should never fail because of background compilation.
                pass
        business = config.get("business", {})
        capabilities = config.get("capabilities", {})
        services = self.get_services()
        service_kb_records = self.get_service_kb_records(active_only=True)
        service_kb_summary: list[dict[str, Any]] = []
        for record in service_kb_records[:120]:
            if not isinstance(record, dict):
                continue
            approved_facts: list[dict[str, Any]] = []
            for fact in (record.get("facts") or []):
                if not isinstance(fact, dict):
                    continue
                status = str(fact.get("status") or "").strip().lower()
                if status != "approved":
                    continue
                approved_facts.append(
                    {
                        "id": str(fact.get("id") or "").strip(),
                        "text": str(fact.get("text") or "").strip(),
                        "source": str(fact.get("source") or "").strip(),
                        "origin": str(fact.get("origin") or "").strip(),
                        "tags": fact.get("tags", []) if isinstance(fact.get("tags"), list) else [],
                        "confidence": float(fact.get("confidence") or 0.0),
                    }
                )
            service_kb_summary.append(
                {
                    "id": str(record.get("id") or "").strip(),
                    "service_id": str(record.get("service_id") or "").strip(),
                    "plugin_id": str(record.get("plugin_id") or "").strip(),
                    "strict_mode": bool(record.get("strict_mode", True)),
                    "version": int(record.get("version") or 0),
                    "published_at": str(record.get("published_at") or "").strip(),
                    "published_by": str(record.get("published_by") or "").strip(),
                    "completeness": record.get("completeness", {}) if isinstance(record.get("completeness"), dict) else {},
                    "facts": approved_facts,
                    "extracted_knowledge": str(record.get("extracted_knowledge") or "").strip(),
                    "generated_extraction_prompt": str(record.get("generated_extraction_prompt") or "").strip(),
                }
            )

        # Build normalized service catalog from services.
        service_catalog = []
        restaurants_summary = []
        for svc in services:
            hours_value = svc.get("hours")
            if not isinstance(hours_value, dict):
                hours_value = {}

            delivery_zones = svc.get("delivery_zones")
            if not isinstance(delivery_zones, list):
                delivery_zones = []
            normalized_zones = [str(zone).strip().lower() for zone in delivery_zones if str(zone).strip()]
            delivers_to_room = any(zone in {"room", "room_delivery", "in_room"} for zone in normalized_zones)
            dine_in_only = "dine_in_only" in normalized_zones or (bool(normalized_zones) and not delivers_to_room)

            service_row = {
                "id": svc.get("id"),
                "name": svc.get("name"),
                "type": svc.get("type", "service"),
                "description": svc.get("description", ""),
                "cuisine": svc.get("cuisine", ""),
                "hours": hours_value,
                "delivery_zones": delivery_zones,
                "is_active": svc.get("is_active", True),
                "phase_id": self._normalize_phase_identifier(svc.get("phase_id")),
                "ticketing_enabled": svc.get("ticketing_enabled", True),
                "ticketing_policy": str(svc.get("ticketing_policy") or "").strip(),
                "service_prompt_pack": copy.deepcopy(svc.get("service_prompt_pack", {})),
                "service_prompt_pack_custom": bool(svc.get("service_prompt_pack_custom", False)),
                "generated_system_prompt": str(svc.get("generated_system_prompt") or "").strip(),
            }
            service_catalog.append(service_row)

            service_type = str(service_row.get("type") or "").strip().lower()
            if any(token in service_type for token in ("restaurant", "dining", "food", "outlet")):
                restaurants_summary.append(
                    {
                        "id": service_row.get("id"),
                        "name": service_row.get("name"),
                        "cuisine": str(service_row.get("cuisine") or service_row.get("description") or "").strip(),
                        "description": service_row.get("description", ""),
                        "hours": hours_value,
                        "is_active": bool(service_row.get("is_active", True)),
                        "delivers_to_room": delivers_to_room,
                        "dine_in_only": dine_in_only,
                    }
                )

        # Build services summary from capabilities
        services_summary = {}
        for cap_id, cap_data in capabilities.items():
            services_summary[cap_id] = cap_data.get("enabled", False)
            if cap_data.get("hours"):
                services_summary[f"{cap_id}_hours"] = cap_data.get("hours")

        return {
            "business_id": business.get("id", ""),
            "hotel_name": business.get("name", "Hotel"),
            "business_name": business.get("name", "Hotel"),
            "bot_name": business.get("bot_name", "Assistant"),
            "city": business.get("city", ""),
            "location": business.get("location", ""),
            "address": business.get("address", ""),
            "timezone": business.get("timezone", "Asia/Kolkata"),
            "currency": business.get("currency", "INR"),
            "language": business.get("language", "en"),
            "timestamp_format": business.get("timestamp_format", "24h"),
            "contact_email": business.get("contact_email", ""),
            "contact_phone": business.get("contact_phone", ""),
            "website": business.get("website", ""),
            "channels": business.get("channels", {}),
            "welcome_message": business.get("welcome_message", "Hello! How can I help you?"),
            "business_type": business.get("type", "hotel"),
            "services": services_summary,
            "restaurants": restaurants_summary,  # Legacy key retained for compatibility.
            "service_catalog": service_catalog,
            "capabilities": capabilities,
            "faq_bank": self.get_faq_bank(),
            "tools": self.get_tools(),
            "workflows": self.get_tools(),
            "intents": self.get_intents(),
            "service_kb_records": service_kb_summary,
            "prompts": config.get("prompts", {}),
            "knowledge_sources": config.get("knowledge_base", {}).get("sources", []),
            "knowledge_notes": config.get("knowledge_base", {}).get("notes", ""),
            "nlu_policy": config.get("knowledge_base", {}).get("nlu_policy", {}),
            "ui_settings": config.get("ui_settings", {}),
            "can_send_multiple_menus": False,
            "human_escalation": capabilities.get("human_escalation", {}).get("enabled", True),
        }

    def is_capability_enabled(self, capability_id: str) -> bool:
        """Check if a specific capability is enabled."""
        normalized_id = self._normalize_identifier(capability_id)
        if not normalized_id:
            return False

        config = self.load_config()
        capabilities = config.get("capabilities", {})
        cap = capabilities.get(normalized_id, {})
        if not cap and capability_id in capabilities:
            cap = capabilities.get(capability_id, {})
        if isinstance(cap, dict) and "enabled" in cap:
            return bool(cap.get("enabled", False))

        # Fallback: infer from enabled intents for industry-agnostic setups.
        capability_to_intents = {
            "food_ordering": ["order_food"],
            "table_booking": ["table_booking", "book_appointment", "appointment_booking"],
            "room_service": ["room_service", "housekeeping"],
            "housekeeping": ["room_service"],
            "transport": ["transport", "transport_request"],
            "spa_booking": ["spa_booking"],
            "menu_request": ["faq", "product_search", "department_info"],
            "human_escalation": ["human_request"],
        }
        for intent_id in capability_to_intents.get(normalized_id, []):
            if self.is_intent_enabled(intent_id):
                return True

        # Fallback: human escalation also maps to enabled escalation tools/modes.
        if normalized_id == "human_escalation":
            for tool in self.get_tools():
                if not tool.get("enabled", False):
                    continue
                tool_id = self._normalize_identifier(tool.get("id"))
                if tool_id in {"ticketing", "live_chat", "human_handoff", "callback", "email_followup"}:
                    return True
            modes = self.get_escalation_config().get("modes", [])
            if isinstance(modes, list) and len(modes) > 0:
                return True

        return False

    def get_welcome_message(self) -> str:
        """Get the welcome message with placeholders replaced."""
        config = self.load_config()
        business = config.get("business", {})
        message = business.get("welcome_message", "Hello! How can I help you today?")

        # Replace placeholders
        message = message.replace("{business_name}", business.get("name", "our business"))
        message = message.replace("{bot_name}", business.get("bot_name", "Assistant"))
        message = message.replace("{city}", business.get("city", ""))

        return message

    def get_service_by_id(self, service_id: str) -> Optional[Dict[str, Any]]:
        """Get a service by its ID."""
        normalized_id = self._normalize_identifier(service_id)
        for svc in self.get_services():
            if self._normalize_identifier(svc.get("id")) == normalized_id:
                return svc
        return None

    def can_deliver_to_room(self, service_id: str) -> bool:
        """Check if a service can deliver to room."""
        service = self.get_service_by_id(service_id)
        if not service:
            return False
        delivery_zones = service.get("delivery_zones", [])
        if not isinstance(delivery_zones, list):
            return False
        return "room" in delivery_zones

    def get_enabled_intent_ids(self) -> set[str]:
        """Return set of enabled intent IDs from current business config."""
        intents = self.get_intents()
        enabled_ids: set[str] = set()
        for intent in intents:
            if not intent.get("enabled", False):
                continue
            intent_id = self._normalize_identifier(intent.get("id"))
            if intent_id:
                enabled_ids.add(intent_id)
            mapped_id = self._normalize_identifier(intent.get("maps_to"))
            if mapped_id:
                enabled_ids.add(mapped_id)
        return enabled_ids

    def is_intent_enabled(self, intent_id: str) -> bool:
        """Check if intent is enabled in admin config."""
        if not intent_id:
            return False
        return self._normalize_identifier(intent_id) in self.get_enabled_intent_ids()

    def get_quick_actions(self, limit: int = 4) -> list[str]:
        """
        Build user quick actions from enabled intents, industry-aware.
        Falls back to static defaults if config is sparse.
        """
        config = self.load_config()
        intents = [i for i in config.get("intents", []) if i.get("enabled", False)]
        excluded_intent_ids = {"greeting", "menu_request"}
        excluded_labels = {"greeting", "knowledge query", "knowledge_query"}

        labels: list[str] = []
        for intent in intents:
            intent_id = self._normalize_identifier(intent.get("id"))
            label = str(intent.get("label") or intent.get("id", "")).strip()
            label_key = self._normalize_identifier(label)
            if not label:
                continue
            if intent_id in excluded_intent_ids:
                continue
            if label_key in excluded_labels:
                continue
            labels.append(label)

        if not labels:
            return ["Need help", "Talk to human"]

        # Keep action chips concise
        compact = labels[: max(1, limit)]
        return compact


# Singleton instance
config_service = ConfigService()
