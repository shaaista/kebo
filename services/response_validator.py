"""
Response Validator

Validates assistant responses against capability constraints before sending.
Adds runtime enforcement for admin NLU do/don't guardrails.
"""

from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, Field

from schemas.chat import ConversationContext, IntentResult, IntentType, MessageRole
from services.kb_direct_lookup_service import kb_direct_lookup_service


class ValidationIssue(BaseModel):
    code: str
    message: str


class ValidationResult(BaseModel):
    valid: bool
    issues: list[ValidationIssue] = Field(default_factory=list)
    action: str = "allow"  # allow | replace
    replacement_response: Optional[str] = None


class ResponseValidator:
    """Lightweight rule validator for bot responses."""

    _ROOM_DELIVERY_TERMS = ("deliver", "delivery", "to your room", "room delivery")
    _INTERCITY_TERMS = ("intercity", "outstation")
    _INTERCITY_USER_TERMS = ("intercity", "outstation", "city to city", "long distance cab", "drop to")
    _SERVICE_ACTION_TERMS = ("book", "reserve", "order", "arrange", "need", "request", "send", "schedule", "confirm")
    _TIME_TERMS = ("am", "pm", "today", "tomorrow", "tonight", "morning", "afternoon", "evening")
    _LOCATION_TERMS = ("room", "terminal", "airport", "address")
    _COUNT_TERMS = ("people", "persons", "guests", "qty", "quantity", "table for")
    _SENSITIVE_TERMS = (
        "admin credential",
        "admin password",
        "database password",
        "backend credential",
        "backend access",
        "api key",
        "secret key",
        "internal roster",
        "staff roster",
        "operations process",
        "internal operational",
        "source code",
        "server access",
        "ssh",
    )
    _SERVICE_AVAILABILITY_TERMS = (
        "available",
        "open",
        "operating",
        "operates",
        "hours",
        "timings",
        "serves",
        "service is on",
        "we provide",
        "we offer",
    )
    _REQUEST_TOKEN_STOPWORDS = {
        "book",
        "booking",
        "reserve",
        "reservation",
        "order",
        "arrange",
        "request",
        "need",
        "want",
        "schedule",
        "confirm",
        "please",
        "hotel",
        "room",
        "today",
        "tomorrow",
        "tonight",
        "morning",
        "afternoon",
        "evening",
        "am",
        "pm",
    }
    _SERVICE_TOKEN_STOPWORDS = {
        "service",
        "services",
        "booking",
        "bookings",
        "support",
        "assistance",
        "request",
        "requests",
        "available",
        "active",
        "provide",
        "provides",
        "offer",
        "offers",
    }
    _GENERIC_SERVICE_ALIASES = {
        "service",
        "services",
        "restaurant",
        "restaurants",
        "menu",
        "booking",
        "order",
        "support",
        "transport",
    }
    _CLARIFICATION_MARKERS = (
        "could you",
        "can you",
        "please provide",
        "please share",
        "share your",
        "provide your",
        "pickup",
        "drop",
        "which one",
        "what time",
        "what date",
        "where",
    )
    _HUMAN_HANDOFF_TERMS = (
        "staff",
        "team",
        "human",
        "agent",
        "connect",
        "handoff",
        "front desk",
        "reception",
    )
    _MEDICAL_REQUEST_TERMS = (
        "medicine",
        "medication",
        "doctor",
        "nurse",
        "prescription",
        "medical",
        "health",
        "dose",
        "dosage",
        "tablet",
        "pill",
        "fever",
        "pain",
        "headache",
        "allergy",
    )
    _MEDICAL_ADVICE_TERMS = (
        "you should take",
        "take ",
        "dosage",
        "dose",
        "twice daily",
        "three times daily",
        "mg",
        "ml",
        "diagnosis",
        "diagnose",
        "prescribe",
        "prescription for",
    )
    _TRANSACTION_CTA_VERBS = (
        "book",
        "booking",
        "reserve",
        "reservation",
        "order",
        "arrange",
        "schedule",
    )
    _TRANSACTION_CTA_PHRASES = (
        "would you like to",
        "if you need",
        "if you want",
        "if you wish",
        "feel free to ask",
        "let me know",
        "i can help",
        "i can assist",
    )
    _TRANSACTIONAL_SERVICE_MARKERS = (
        "booking",
        "reservation",
        "dining",
        "restaurant",
        "order",
        "spa",
        "room service",
        "housekeeping",
        "maintenance",
        "transport",
        "transfer",
    )
    _EXTERNAL_HOTEL_MARKERS = (
        "other hotel",
        "another hotel",
        "different hotel",
        "recommend",
        "suggest",
        "best hotel",
    )
    _CURRENT_HOTEL_SCOPE_MARKERS = (
        "your hotel",
        "ur hotel",
        "this hotel",
        "current hotel",
        "near your hotel",
        "near ur hotel",
        "around your hotel",
        "around ur hotel",
    )
    _LOCAL_AREA_MARKERS = (
        "nearby",
        "near",
        "around",
        "close by",
        "sightseeing",
        "attractions",
        "places to visit",
        "things to do",
        "beach",
    )
    _UNAVAILABLE_INFO_MARKERS = (
        "don't have specific information",
        "do not have specific information",
        "don't have information",
        "do not have information",
        "don't know",
        "not sure",
        "at the moment",
        "connect you with our staff",
        "connect you with our team",
        "personalized recommendations",
    )
    _PHASE_ALIASES = {
        "prebooking": "pre_booking",
        "booking": "pre_checkin",
        "precheckin": "pre_checkin",
        "duringstay": "during_stay",
        "instay": "during_stay",
        "in_stay": "during_stay",
        "postcheckout": "post_checkout",
    }

    def validate(
        self,
        response_text: str,
        intent_result: IntentResult,
        context: ConversationContext,
        capabilities_summary: dict,
        capability_check_allowed: bool,
        capability_reason: str,
    ) -> ValidationResult:
        response_lower = (response_text or "").lower()
        latest_user_message = self._latest_user_message(context).lower()
        issues: list[ValidationIssue] = []
        nlu_policy = capabilities_summary.get("nlu_policy", {}) if isinstance(capabilities_summary, dict) else {}
        dos = nlu_policy.get("dos", []) if isinstance(nlu_policy, dict) else []
        donts = nlu_policy.get("donts", []) if isinstance(nlu_policy, dict) else []

        # Rule 0: Never disclose or assist with sensitive internal data/access.
        if self._is_sensitive_internal_request(latest_user_message):
            issues.append(
                ValidationIssue(
                    code="sensitive_internal_request",
                    message="User requested internal credentials/operations data.",
                )
            )
            return ValidationResult(
                valid=False,
                issues=issues,
                action="replace",
                replacement_response=(
                    "I can't share internal credentials, private staff records, or operational access details. "
                    "If you need legitimate support, I can connect you with the authorized team."
                ),
            )

        # Rule 0b: Response itself must not leak sensitive content.
        if any(term in response_lower for term in self._SENSITIVE_TERMS):
            issues.append(
                ValidationIssue(
                    code="sensitive_output_leak",
                    message="Response appears to leak sensitive/internal terms.",
                )
            )
            return ValidationResult(
                valid=False,
                issues=issues,
                action="replace",
                replacement_response=(
                    "I can't share internal credentials or private operational details. "
                    "I can connect you with authorized support if needed."
                ),
            )

        # Rule 0c: Never provide medication diagnosis/dosage advice.
        if self._is_medical_request(latest_user_message) and self._looks_like_medical_advice(response_lower):
            issues.append(
                ValidationIssue(
                    code="medical_advice_guardrail",
                    message="Response appears to provide medical diagnosis or dosage advice.",
                )
            )
            return ValidationResult(
                valid=False,
                issues=issues,
                action="replace",
                replacement_response=(
                    "I'm not able to provide medical diagnosis or dosage guidance. "
                    "I can connect you with our team right away for safe assistance."
                ),
            )

        # Rule 1: If capability layer denied the request, response must not promise execution.
        if not capability_check_allowed and self._looks_like_promise(response_lower):
            issues.append(
                ValidationIssue(
                    code="capability_violation",
                    message="Response promises an action that was denied by capability checks.",
                )
            )
            return ValidationResult(
                valid=False,
                issues=issues,
                action="replace",
                replacement_response=capability_reason,
            )

        # Rule 1a: In FAQ fallback, avoid promising completion for requests that
        # are not represented in active configured services.
        unconfigured_promise_issue = self._check_unconfigured_service_promise(
            latest_user_message=latest_user_message,
            response_lower=response_lower,
            capabilities_summary=capabilities_summary,
            intent_result=intent_result,
        )
        if unconfigured_promise_issue is not None:
            issues.append(unconfigured_promise_issue)
            return ValidationResult(
                valid=False,
                issues=issues,
                action="replace",
                replacement_response=(
                    "That request is not configured for instant completion in this bot right now. "
                    "I can connect you with our staff team to assist manually."
                ),
            )

        # Rule 1b: For unsupported service requests, avoid clarification loops.
        # Offer human handoff instead of collecting details the bot cannot fulfill.
        unconfigured_clarification_issue = self._check_unconfigured_service_clarification(
            latest_user_message=latest_user_message,
            response_lower=response_lower,
            capabilities_summary=capabilities_summary,
            intent_result=intent_result,
        )
        if unconfigured_clarification_issue is not None:
            issues.append(unconfigured_clarification_issue)
            return ValidationResult(
                valid=False,
                issues=issues,
                action="replace",
                replacement_response=(
                    "I don't have enough information in the current knowledge base to complete that here yet. "
                    "If you want, I can connect you with our staff team right away."
                ),
            )

        # Rule 1d: Avoid CTA text that invites transactional actions outside current phase services.
        phase_cta_rewrite = self._build_phase_safe_info_response_if_needed(
            response_text=response_text,
            response_lower=response_lower,
            latest_user_message=latest_user_message,
            capabilities_summary=capabilities_summary,
            context=context,
            intent_result=intent_result,
        )
        if phase_cta_rewrite is not None:
            issues.append(
                ValidationIssue(
                    code="phase_unavailable_transaction_cta",
                    message="Response invites transactional action outside current phase services.",
                )
            )
            return ValidationResult(
                valid=False,
                issues=issues,
                action="replace",
                replacement_response=phase_cta_rewrite,
            )

        # Rule 1e: Keep recommendation scope on the current property only.
        scope_rewrite = self._build_current_hotel_scope_response_if_needed(
            latest_user_message=latest_user_message,
            capabilities_summary=capabilities_summary,
        )
        if scope_rewrite is not None:
            issues.append(
                ValidationIssue(
                    code="external_hotel_scope_enforcement",
                    message="Request asks for other hotels; response must stay scoped to current hotel.",
                )
            )
            return ValidationResult(
                valid=False,
                issues=issues,
                action="replace",
                replacement_response=scope_rewrite,
            )

        # Rule 1f: Keep nearby-attractions questions helpful for the current hotel area.
        nearby_rewrite = self._build_local_area_info_response_if_needed(
            response_text=response_text,
            response_lower=response_lower,
            latest_user_message=latest_user_message,
            capabilities_summary=capabilities_summary,
            context=context,
        )
        if nearby_rewrite is not None:
            issues.append(
                ValidationIssue(
                    code="nearby_area_info_rewrite",
                    message="Nearby area query received an unhelpful unavailable response.",
                )
            )
            return ValidationResult(
                valid=False,
                issues=issues,
                action="replace",
                replacement_response=nearby_rewrite,
            )

        # Rule 1c: Apply configured DON'T policy rules at runtime.
        dont_violation = self._check_dont_rules(
            donts=donts,
            latest_user_message=latest_user_message,
            response_lower=response_lower,
            capabilities_summary=capabilities_summary,
        )
        if dont_violation is not None:
            issues.append(dont_violation)
            return ValidationResult(
                valid=False,
                issues=issues,
                action="replace",
                replacement_response=(
                    "I want to be accurate. I can help only with configured services and policies, "
                    "or connect you with our team for special requests."
                ),
            )

        # Rule 2: Dine-in only restaurants must not be described as room-deliverable.
        for restaurant in capabilities_summary.get("restaurants", []):
            name = (restaurant.get("name") or "").lower()
            if not name:
                continue
            if restaurant.get("dine_in_only") and name in response_lower:
                if self._alias_line_mentions_room_delivery(response_text, name):
                    issues.append(
                        ValidationIssue(
                            code="dine_in_delivery_contradiction",
                            message=f"Response implies room delivery for dine-in-only restaurant '{name}'.",
                        )
                    )

        # Rule 2b: Inactive services should not be presented as available.
        for service in capabilities_summary.get("service_catalog", []):
            if not isinstance(service, dict):
                continue
            if bool(service.get("is_active", True)):
                continue
            aliases = self._service_aliases_for_validation(service)
            if not aliases:
                continue
            if not any(self._contains_service_alias(response_lower, alias) for alias in aliases):
                continue
            if any(term in response_lower for term in ("unavailable", "inactive", "closed", "not available")):
                continue
            if not self._looks_like_availability_or_promise(response_lower):
                continue
            issues.append(
                ValidationIssue(
                    code="inactive_service_promoted",
                    message=f"Response references inactive service '{aliases[0]}' as available.",
                )
            )

        # Rule 3: Intercity should not be promised if disabled.
        services = capabilities_summary.get("services", {})
        if not services.get("intercity_cab", False):
            if any(term in response_lower for term in self._INTERCITY_TERMS):
                if "not available" not in response_lower and "do not" not in response_lower:
                    issues.append(
                        ValidationIssue(
                            code="intercity_contradiction",
                            message="Response appears to promise intercity travel despite capability restrictions.",
                        )
                    )

        # Rule 4: Apply configured DO policy rules for detail confirmation.
        do_violation = self._check_do_rules(
            dos=dos,
            latest_user_message=latest_user_message,
            response_lower=response_lower,
            context=context,
            intent_result=intent_result,
        )
        if do_violation is not None:
            issues.append(do_violation)
            missing_bits = self._missing_request_details(latest_user_message, context, intent_result)
            missing_text = ", ".join(missing_bits) if missing_bits else "timing and required details"
            return ValidationResult(
                valid=False,
                issues=issues,
                action="replace",
                replacement_response=f"To help accurately, could you share {missing_text}?",
            )

        if issues:
            return ValidationResult(
                valid=False,
                issues=issues,
                action="replace",
                replacement_response=(
                    "I want to be accurate here. I can help with available in-hotel services "
                    "or connect you with our team for special requests."
                ),
            )

        return ValidationResult(valid=True)

    def _latest_user_message(self, context: ConversationContext) -> str:
        for msg in reversed(context.messages):
            if msg.role == MessageRole.USER:
                return str(msg.content or "")
        return ""

    def _is_sensitive_internal_request(self, user_message_lower: str) -> bool:
        if not user_message_lower:
            return False
        return any(term in user_message_lower for term in self._SENSITIVE_TERMS)

    def _check_dont_rules(
        self,
        donts: list,
        latest_user_message: str,
        response_lower: str,
        capabilities_summary: dict,
    ) -> Optional[ValidationIssue]:
        """Apply deterministic checks inferred from admin DON'T rules."""
        normalized_donts = [str(rule).strip().lower() for rule in donts if str(rule).strip()]
        if not normalized_donts:
            return None

        for rule in normalized_donts:
            # Explicit intercity guardrail.
            if "intercity" in rule and ("do not" in rule or "don't" in rule):
                user_mentions_intercity = any(term in latest_user_message for term in self._INTERCITY_USER_TERMS)
                if user_mentions_intercity:
                    services = capabilities_summary.get("services", {})
                    intercity_enabled = bool(services.get("intercity_cab", False))
                    if not intercity_enabled and self._looks_like_promise(response_lower):
                        return ValidationIssue(
                            code="policy_dont_intercity_commit",
                            message="DON'T rule forbids committing intercity transfer when disabled.",
                        )

            # Internal disclosure guardrail.
            if ("internal" in rule or "credential" in rule or "operations" in rule) and any(
                token in response_lower for token in self._SENSITIVE_TERMS
            ):
                return ValidationIssue(
                    code="policy_dont_internal_disclosure",
                    message="DON'T rule forbids sharing internal details.",
                )

        return None

    def _check_do_rules(
        self,
        dos: list,
        latest_user_message: str,
        response_lower: str,
        context: ConversationContext,
        intent_result: IntentResult,
    ) -> Optional[ValidationIssue]:
        """Apply deterministic checks inferred from admin DO rules."""
        normalized_dos = [str(rule).strip().lower() for rule in dos if str(rule).strip()]
        if not normalized_dos:
            return None

        needs_detail_confirmation = any(
            ("confirm" in rule and "timing" in rule and "location" in rule)
            or ("timing" in rule and "location" in rule and "count" in rule)
            for rule in normalized_dos
        )
        if not needs_detail_confirmation:
            return None

        if not self._looks_like_service_request(latest_user_message):
            return None

        if intent_result.intent not in {IntentType.ORDER_FOOD, IntentType.ROOM_SERVICE, IntentType.TABLE_BOOKING}:
            return None
        if intent_result.intent == IntentType.ORDER_FOOD:
            # Food-order handlers already collect/confirm item-level details.
            # Avoid generic policy replacement that can overwrite valid summaries.
            return None
        if intent_result.intent == IntentType.TABLE_BOOKING and context.pending_action in {
            "select_service",
            "select_restaurant",
            "collect_booking_party_size",
            "collect_booking_time",
            "confirm_booking",
        }:
            # Keep booking slot-filling prompts deterministic from handler flow.
            return None
        if intent_result.intent == IntentType.TABLE_BOOKING and self._is_room_booking_context(
            latest_user_message=latest_user_message,
            context=context,
            intent_result=intent_result,
        ):
            # Room-booking flow collects room type + stay dates + guest count;
            # generic time/count policy replacement breaks this flow.
            return None

        missing = self._missing_request_details(latest_user_message, context, intent_result)
        if not missing:
            return None

        asks_detail = (
            "?" in response_lower
            and any(
                token in response_lower
                for token in ("time", "timing", "when", "room", "location", "where", "how many", "count", "quantity")
            )
        )
        if asks_detail:
            return None

        return ValidationIssue(
            code="policy_do_missing_detail_confirmation",
            message="DO rule expects timing/location/count confirmation for service requests.",
        )

    def _looks_like_service_request(self, latest_user_message: str) -> bool:
        if not latest_user_message:
            return False
        if not any(term in latest_user_message for term in self._SERVICE_ACTION_TERMS):
            return False
        if latest_user_message.strip().startswith(("what", "when", "where", "how")) and "book" not in latest_user_message:
            return False
        return True

    def _missing_request_details(
        self,
        latest_user_message: str,
        context: ConversationContext,
        intent_result: IntentResult,
    ) -> list[str]:
        missing: list[str] = []
        has_time = any(term in latest_user_message for term in self._TIME_TERMS) or bool(
            re.search(r"\b\d{1,2}(:\d{2})?\s?(am|pm)?\b", latest_user_message)
        )
        has_location = any(term in latest_user_message for term in self._LOCATION_TERMS) or bool(context.room_number)
        has_count = any(term in latest_user_message for term in self._COUNT_TERMS) or bool(
            re.search(r"\b\d+\b", latest_user_message)
        )

        if intent_result.intent == IntentType.TABLE_BOOKING:
            if self._is_room_booking_context(
                latest_user_message=latest_user_message,
                context=context,
                intent_result=intent_result,
            ):
                has_room_type = any(
                    marker in latest_user_message
                    for marker in ("room type", "king", "twin", "suite", "premier", "ultimate", "reserve", "prestige")
                ) or bool(
                    self._first_non_empty(
                        context.pending_data.get("room_type") if isinstance(context.pending_data, dict) else "",
                        context.pending_data.get("room_name") if isinstance(context.pending_data, dict) else "",
                    )
                )
                has_stay_dates = (
                    "check in" in latest_user_message
                    or "check-out" in latest_user_message
                    or "check out" in latest_user_message
                    or bool(re.search(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", latest_user_message))
                    or bool(re.search(r"\b\d{1,2}\s*[-/]\s*\d{1,2}\b", latest_user_message))
                    or bool(
                        self._first_non_empty(
                            context.pending_data.get("stay_checkin_date") if isinstance(context.pending_data, dict) else "",
                            context.pending_data.get("stay_checkout_date") if isinstance(context.pending_data, dict) else "",
                            context.pending_data.get("check_in") if isinstance(context.pending_data, dict) else "",
                            context.pending_data.get("check_out") if isinstance(context.pending_data, dict) else "",
                            context.pending_data.get("stay_date_range") if isinstance(context.pending_data, dict) else "",
                        )
                    )
                )
                has_guest_count = has_count or bool(
                    self._first_non_empty(
                        context.pending_data.get("guest_count") if isinstance(context.pending_data, dict) else "",
                        context.pending_data.get("party_size") if isinstance(context.pending_data, dict) else "",
                    )
                )
                if not has_room_type:
                    missing.append("your preferred room type")
                if not has_stay_dates:
                    missing.append("your check-in and check-out dates")
                if not has_guest_count:
                    missing.append("the number of guests")
                return missing
            if not has_time:
                missing.append("your preferred time")
            if not has_count:
                missing.append("the number of guests")
            return missing

        if intent_result.intent == IntentType.ROOM_SERVICE:
            if not has_location:
                missing.append("your location or room number")
            if not has_time:
                missing.append("your preferred time")
            return missing

        # ORDER_FOOD and fallback service intents.
        if not has_time:
            missing.append("your preferred time")
        if not has_location:
            missing.append("your location or room number")
        if not has_count:
            missing.append("the number of people/items")
        return missing

    @staticmethod
    def _first_non_empty(*values: object) -> str:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return ""

    def _is_room_booking_context(
        self,
        *,
        latest_user_message: str,
        context: ConversationContext,
        intent_result: IntentResult,
    ) -> bool:
        entities = intent_result.entities if isinstance(intent_result.entities, dict) else {}
        entity_blob = " ".join(
            [
                str(entities.get("booking_sub_category") or ""),
                str(entities.get("booking_type") or ""),
                str(entities.get("custom_intent") or ""),
                str(entities.get("resolved_intent") or ""),
                str(entities.get("service_name") or ""),
            ]
        ).lower()
        if any(marker in entity_blob for marker in ("room_booking", "room", "suite", "stay")):
            return True

        pending_action = str(getattr(context, "pending_action", "") or "").strip().lower()
        if pending_action in {
            "collect_room_booking_details",
            "select_room_type",
            "confirm_room_booking",
            "confirm_room_availability_check",
        }:
            return True

        pending_data = context.pending_data if isinstance(context.pending_data, dict) else {}
        if any(
            key in pending_data
            for key in (
                "room_type",
                "room_name",
                "stay_checkin_date",
                "stay_checkout_date",
                "check_in",
                "check_out",
                "stay_date_range",
                "guest_count",
            )
        ):
            return True

        text = str(latest_user_message or "").strip().lower()
        if not text:
            return False
        has_room_marker = any(marker in text for marker in ("room", "suite", "stay", "check in", "check-out", "check out", "checkin", "checkout"))
        has_booking_marker = any(marker in text for marker in ("book", "booking", "reserve", "need", "want"))
        return has_room_marker and has_booking_marker

    def _service_aliases_for_validation(self, service: dict) -> list[str]:
        aliases: list[str] = []
        service_name = str(service.get("name") or "").strip().lower()
        service_id = str(service.get("id") or "").strip().lower().replace("_", " ")

        if service_name:
            aliases.append(service_name)

        if service_id and service_id != service_name and self._is_specific_service_alias(service_id):
            aliases.append(service_id)

        # Keep insertion order while removing duplicates.
        deduped: list[str] = []
        seen: set[str] = set()
        for alias in aliases:
            if alias and alias not in seen:
                deduped.append(alias)
                seen.add(alias)
        return deduped

    def _is_specific_service_alias(self, alias: str) -> bool:
        compact = alias.strip().lower()
        if not compact:
            return False
        if compact in self._GENERIC_SERVICE_ALIASES:
            return False
        # IDs with separators/digits are usually specific identifiers.
        if any(ch in compact for ch in ("_", "-", "/")) or any(ch.isdigit() for ch in compact):
            return True
        # Keep short aliases like "ird"; avoid broad plain words.
        return len(compact) <= 5

    @staticmethod
    def _contains_service_alias(response_lower: str, alias: str) -> bool:
        return bool(re.search(rf"\b{re.escape(alias)}\b", response_lower))

    def _alias_line_mentions_room_delivery(self, response_text: str, alias: str) -> bool:
        """
        Scope delivery checks to local context near a specific alias, instead of
        scanning the whole response globally.
        """
        response_lower = str(response_text or "").lower()
        alias_lower = str(alias or "").strip().lower()
        if not response_lower or not alias_lower:
            return False

        lines = [line.strip() for line in response_lower.splitlines() if line.strip()]
        for line in lines:
            if alias_lower in line and any(term in line for term in self._ROOM_DELIVERY_TERMS):
                return True
        return False

    def _looks_like_availability_or_promise(self, response_lower: str) -> bool:
        if self._looks_like_promise(response_lower):
            return True
        return any(term in response_lower for term in self._SERVICE_AVAILABILITY_TERMS)

    def _is_medical_request(self, user_message_lower: str) -> bool:
        if not user_message_lower:
            return False
        return any(term in user_message_lower for term in self._MEDICAL_REQUEST_TERMS)

    def _looks_like_medical_advice(self, response_lower: str) -> bool:
        if not response_lower:
            return False
        # Allow explicit safety disclaimers.
        if any(
            phrase in response_lower
            for phrase in (
                "can't provide medical",
                "cannot provide medical",
                "not able to provide medical",
                "connect you with our team",
                "contact emergency services",
            )
        ):
            return False
        if any(term in response_lower for term in self._MEDICAL_ADVICE_TERMS):
            return True
        # Catch dosage-like numeric patterns, e.g. "500 mg every 6 hours".
        return bool(re.search(r"\b\d+\s*(mg|ml)\b", response_lower))

    @staticmethod
    def _looks_like_promise(response_lower: str) -> bool:
        """
        Detect language that suggests execution/commitment.
        Conservative by design to reduce false promises.
        """
        return bool(
            re.search(
                r"\b(i('?ll| will| can) (arrange|book|confirm|send|deliver)|"
                r"your (order|booking) (is|has been) (confirmed|placed)|"
                r"done|confirmed)\b",
                response_lower,
            )
        )

    def _check_unconfigured_service_promise(
        self,
        *,
        latest_user_message: str,
        response_lower: str,
        capabilities_summary: dict,
        intent_result: IntentResult,
    ) -> Optional[ValidationIssue]:
        if intent_result.intent != IntentType.FAQ:
            return None
        if not self._looks_like_service_request(latest_user_message):
            return None
        if any(term in latest_user_message for term in self._INTERCITY_USER_TERMS):
            return None
        if not self._looks_like_promise(response_lower):
            return None
        if self._request_matches_active_service_catalog(
            latest_user_message=latest_user_message,
            capabilities_summary=capabilities_summary,
        ):
            return None
        return ValidationIssue(
            code="promise_for_unconfigured_service_request",
            message="Response promises completion for request not represented in active service catalog.",
        )

    def _check_unconfigured_service_clarification(
        self,
        *,
        latest_user_message: str,
        response_lower: str,
        capabilities_summary: dict,
        intent_result: IntentResult,
    ) -> Optional[ValidationIssue]:
        if intent_result.intent != IntentType.FAQ:
            return None
        if not self._looks_like_service_request(latest_user_message):
            return None
        if any(term in latest_user_message for term in self._INTERCITY_USER_TERMS):
            return None
        if self._request_matches_active_service_catalog(
            latest_user_message=latest_user_message,
            capabilities_summary=capabilities_summary,
        ):
            return None
        if self._response_has_handoff_offer(response_lower):
            return None
        if not self._looks_like_clarification_question(response_lower):
            return None
        return ValidationIssue(
            code="clarification_for_unconfigured_service_request",
            message="Response asks clarifying details for unsupported service instead of offering handoff.",
        )

    @staticmethod
    def _tokenize_request_terms(message: str) -> set[str]:
        return {
            token
            for token in re.findall(r"[a-z0-9]+", str(message or "").lower())
            if len(token) >= 3 and token not in ResponseValidator._REQUEST_TOKEN_STOPWORDS and not token.isdigit()
        }

    @staticmethod
    def _tokenize_service_terms(service: dict) -> set[str]:
        service_text = " ".join(
            [
                str(service.get("id") or ""),
                str(service.get("name") or ""),
                str(service.get("type") or ""),
                str(service.get("description") or service.get("cuisine") or ""),
            ]
        ).strip().lower()
        return {
            token
            for token in re.findall(r"[a-z0-9]+", service_text)
            if len(token) >= 3 and token not in ResponseValidator._SERVICE_TOKEN_STOPWORDS and not token.isdigit()
        }

    def _request_matches_active_service_catalog(
        self,
        *,
        latest_user_message: str,
        capabilities_summary: dict,
    ) -> bool:
        request_tokens = self._tokenize_request_terms(latest_user_message)
        if len(request_tokens) < 2:
            return True

        service_catalog = capabilities_summary.get("service_catalog", []) if isinstance(capabilities_summary, dict) else []
        service_tokens: set[str] = set()
        for service in service_catalog:
            if not isinstance(service, dict):
                continue
            if not bool(service.get("is_active", True)):
                continue
            service_tokens.update(self._tokenize_service_terms(service))

        if not service_tokens:
            return False
        return bool(request_tokens & service_tokens)

    def _looks_like_clarification_question(self, response_lower: str) -> bool:
        text = str(response_lower or "").strip().lower()
        if not text:
            return False
        if "?" not in text and not any(marker in text for marker in self._CLARIFICATION_MARKERS):
            return False
        return any(marker in text for marker in self._CLARIFICATION_MARKERS) or text.endswith("?")

    def _response_has_handoff_offer(self, response_lower: str) -> bool:
        text = str(response_lower or "").strip().lower()
        if not text:
            return False
        return any(term in text for term in self._HUMAN_HANDOFF_TERMS)

    @classmethod
    def _normalize_phase_identifier(cls, value: str) -> str:
        compact = str(value or "").strip().lower().replace(" ", "_")
        return cls._PHASE_ALIASES.get(compact, compact)

    @classmethod
    def _resolve_current_phase_id(cls, context: ConversationContext) -> str:
        pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        integration = pending.get("_integration", {})
        if isinstance(integration, dict):
            phase = cls._normalize_phase_identifier(str(integration.get("phase") or ""))
            if phase:
                return phase
        return cls._normalize_phase_identifier(str(pending.get("phase") or ""))

    @classmethod
    def _phase_label(cls, phase_id: str) -> str:
        text = str(phase_id or "").strip().replace("_", " ")
        return text.title() if text else "Current"

    def _response_invites_transaction_action(self, response_lower: str) -> bool:
        text = str(response_lower or "").strip().lower()
        if not text:
            return False
        if any(marker in text for marker in ("not available for", "not available in this phase")):
            return False
        has_verb = any(verb in text for verb in self._TRANSACTION_CTA_VERBS)
        has_phrase = any(phrase in text for phrase in self._TRANSACTION_CTA_PHRASES)
        if has_verb and has_phrase:
            return True
        return bool(re.search(r"\b(to|for)\s+(book|reserve|order|arrange|schedule)\b", text))

    def _strip_transaction_cta_sentences(self, response_text: str) -> str:
        text = str(response_text or "").strip()
        if not text:
            return ""
        sentences = [segment.strip() for segment in re.split(r"(?<=[.!?])\s+", text) if segment.strip()]
        kept: list[str] = []
        for sentence in sentences:
            lowered = sentence.lower()
            if any(verb in lowered for verb in self._TRANSACTION_CTA_VERBS) and (
                any(phrase in lowered for phrase in self._TRANSACTION_CTA_PHRASES)
                or "to book" in lowered
                or "to reserve" in lowered
                or "to order" in lowered
            ):
                continue
            kept.append(sentence)
        return " ".join(kept).strip()

    def _phase_has_transactional_services(self, phase_services: list[dict]) -> bool:
        for service in phase_services:
            if not isinstance(service, dict):
                continue
            text = " ".join(
                [
                    str(service.get("id") or ""),
                    str(service.get("name") or ""),
                    str(service.get("type") or ""),
                    str(service.get("description") or service.get("cuisine") or ""),
                ]
            ).strip().lower()
            if not text:
                continue
            if any(marker in text for marker in self._TRANSACTIONAL_SERVICE_MARKERS):
                return True
        return False

    def _request_matches_phase_services(self, latest_user_message: str, phase_services: list[dict]) -> bool:
        request_tokens = self._tokenize_request_terms(latest_user_message)
        if len(request_tokens) < 2:
            return True
        phase_tokens: set[str] = set()
        for service in phase_services:
            if not isinstance(service, dict):
                continue
            if not bool(service.get("is_active", True)):
                continue
            phase_tokens.update(self._tokenize_service_terms(service))
        if not phase_tokens:
            return False
        return bool(request_tokens & phase_tokens)

    def _build_phase_safe_info_response_if_needed(
        self,
        *,
        response_text: str,
        response_lower: str,
        latest_user_message: str,
        capabilities_summary: dict,
        context: ConversationContext,
        intent_result: IntentResult,
    ) -> Optional[str]:
        if intent_result.intent not in {IntentType.FAQ, IntentType.MENU_REQUEST, IntentType.GREETING}:
            return None
        if not self._response_invites_transaction_action(response_lower):
            return None

        phase_id = self._resolve_current_phase_id(context)
        if not phase_id:
            return None

        service_catalog = capabilities_summary.get("service_catalog", []) if isinstance(capabilities_summary, dict) else []
        active_services = [
            service
            for service in service_catalog
            if isinstance(service, dict) and bool(service.get("is_active", True))
        ]
        phase_services = [
            service for service in active_services
            if self._normalize_phase_identifier(str(service.get("phase_id") or "")) == phase_id
        ]
        if not phase_services:
            return None

        phase_has_transactional = self._phase_has_transactional_services(phase_services)
        if phase_has_transactional and self._request_matches_phase_services(latest_user_message, phase_services):
            return None

        cleaned = self._strip_transaction_cta_sentences(response_text)
        phase_label = self._phase_label(phase_id)
        business_name = str(
            capabilities_summary.get("hotel_name")
            or capabilities_summary.get("business_name")
            or "our hotel"
        ).strip()
        current_phase_service_names = [
            str(service.get("name") or "").strip()
            for service in phase_services
            if str(service.get("name") or "").strip()
        ][:4]

        matched_service = self._match_requested_service_from_catalog(
            latest_user_message=latest_user_message,
            service_catalog=active_services,
        )
        if matched_service is not None:
            service_phase_id = self._normalize_phase_identifier(str(matched_service.get("phase_id") or ""))
            if service_phase_id and service_phase_id != phase_id:
                service_name = str(matched_service.get("name") or matched_service.get("id") or "This service").strip()
                target_phase_label = self._phase_label(service_phase_id)
                timing_hint = self._phase_transition_timing_hint(
                    current_phase_id=phase_id,
                    service_phase_id=service_phase_id,
                )
                parts: list[str] = []
                if cleaned and not self._looks_like_unavailable_info_response(cleaned.lower()):
                    parts.append(cleaned)
                else:
                    parts.append(f"Yes, {service_name} is available at {business_name}.")

                availability_line = f"{service_name} is available in {target_phase_label} phase."
                if timing_hint:
                    availability_line += f" You can request it {timing_hint}."
                parts.append(availability_line)
                if current_phase_service_names:
                    parts.append(
                        f"For now, in {phase_label} phase, I can help with {', '.join(current_phase_service_names)}."
                    )
                return " ".join(part.strip() for part in parts if part and part.strip()).strip()

        suffix = f"I can share information, and action requests are available only for {phase_label} phase services."
        if current_phase_service_names:
            suffix += f" Right now, I can help with {', '.join(current_phase_service_names)}."
        if cleaned:
            return f"{cleaned} {suffix}".strip()
        return suffix

    @classmethod
    def _phase_transition_timing_hint(cls, *, current_phase_id: str, service_phase_id: str) -> str:
        current_norm = cls._normalize_phase_identifier(current_phase_id)
        service_norm = cls._normalize_phase_identifier(service_phase_id)
        if not current_norm or not service_norm or current_norm == service_norm:
            return ""

        if current_norm == "pre_booking":
            if service_norm == "pre_checkin":
                return "after your booking is confirmed"
            if service_norm in {"during_stay", "post_checkout"}:
                return "after check-in"
        if current_norm == "pre_checkin":
            if service_norm == "during_stay":
                return "once you check in"
            if service_norm == "post_checkout":
                return "after checkout"
        if current_norm == "during_stay" and service_norm == "post_checkout":
            return "after checkout"
        return ""

    def _match_requested_service_from_catalog(
        self,
        *,
        latest_user_message: str,
        service_catalog: list[dict],
    ) -> Optional[dict]:
        request_tokens = self._tokenize_request_terms(latest_user_message)
        if not request_tokens:
            return None

        best_service: Optional[dict] = None
        best_score = 0.0
        for service in service_catalog:
            if not isinstance(service, dict):
                continue
            service_tokens = self._tokenize_service_terms(service)
            if not service_tokens:
                continue
            overlap = len(request_tokens & service_tokens) / max(1, len(request_tokens))
            if overlap > best_score:
                best_score = overlap
                best_service = service
        if best_service is None or best_score < 0.34:
            return None
        return best_service

    @classmethod
    def _is_external_hotel_query(cls, latest_user_message: str) -> bool:
        text = str(latest_user_message or "").strip().lower()
        if not text or "hotel" not in text:
            return False
        if any(marker in text for marker in cls._CURRENT_HOTEL_SCOPE_MARKERS):
            return False
        if any(marker in text for marker in cls._EXTERNAL_HOTEL_MARKERS):
            return True
        return (
            ("recommend" in text or "suggest" in text or "options" in text)
            and "hotel" in text
            and not any(marker in text for marker in ("near your hotel", "near ur hotel", "your hotel"))
        )

    def _build_current_hotel_scope_response_if_needed(
        self,
        *,
        latest_user_message: str,
        capabilities_summary: dict,
    ) -> Optional[str]:
        if not self._is_external_hotel_query(latest_user_message):
            return None
        business_name = str(
            capabilities_summary.get("hotel_name")
            or capabilities_summary.get("business_name")
            or "our hotel"
        ).strip()
        city = str(capabilities_summary.get("city") or "").strip()
        city_text = f" in {city}" if city else ""
        return (
            f"I can help with {business_name}{city_text} only. "
            "I can share our room options, amenities, dining, and nearby attractions around this property."
        )

    @classmethod
    def _is_local_area_query(cls, latest_user_message: str) -> bool:
        text = str(latest_user_message or "").strip().lower()
        if not text:
            return False
        if not any(marker in text for marker in cls._LOCAL_AREA_MARKERS):
            return False
        if any(marker in text for marker in cls._CURRENT_HOTEL_SCOPE_MARKERS):
            return True
        if any(marker in text for marker in ("your hotel", "ur hotel", "this hotel", "our hotel")):
            return True
        return any(marker in text for marker in ("beach", "sightseeing", "attractions", "things to do"))

    @classmethod
    def _looks_like_unavailable_info_response(cls, response_lower: str) -> bool:
        text = str(response_lower or "").strip().lower()
        if not text:
            return False
        if any(marker in text for marker in cls._UNAVAILABLE_INFO_MARKERS):
            return True
        if ("sorry" in text or "apologize" in text) and ("don't" in text or "do not" in text or "cannot" in text):
            return True
        return False

    def _build_local_area_info_response_if_needed(
        self,
        *,
        response_text: str,
        response_lower: str,
        latest_user_message: str,
        capabilities_summary: dict,
        context: ConversationContext,
    ) -> Optional[str]:
        if self._is_external_hotel_query(latest_user_message):
            return None
        if not self._is_local_area_query(latest_user_message):
            return None
        if not self._looks_like_unavailable_info_response(response_lower):
            return None

        business_name = str(
            capabilities_summary.get("hotel_name")
            or capabilities_summary.get("business_name")
            or "our hotel"
        ).strip()
        city = str(capabilities_summary.get("city") or "").strip()
        tenant_id = str(context.hotel_code or "default").strip() or "default"

        nearby_summary = ""
        try:
            nearby_result = kb_direct_lookup_service.answer_question(
                "nearby attractions",
                tenant_id=tenant_id,
            )
            if nearby_result.handled and str(nearby_result.answer or "").strip():
                nearby_summary = re.sub(r"\s+", " ", str(nearby_result.answer or "").strip())
        except Exception:
            nearby_summary = ""

        parts = [f"Yes, I can help with nearby sightseeing around {business_name}."]
        if nearby_summary:
            parts.append(nearby_summary)
        elif city:
            parts.append(
                f"I can guide you with popular attractions around {city} and what is practical from the hotel area."
            )
        else:
            parts.append("I can guide you with attractions around the hotel area.")

        if "beach" in str(latest_user_message or "").lower():
            parts.append(
                "For beach plans specifically, I can share practical options near the hotel area and expected travel time windows."
            )
        return " ".join(part.strip() for part in parts if part and part.strip()).strip()


# Global instance
response_validator = ResponseValidator()
