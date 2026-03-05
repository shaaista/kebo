"""
Transport Handler

Handles transport-related requests: local cabs, airport transfers,
and intercity travel. Detected via message keywords rather than a
dedicated IntentType.
"""

import re
from typing import Any

from handlers.base_handler import BaseHandler, HandlerResult
from schemas.chat import ConversationState, IntentResult, ConversationContext
from services.config_service import config_service

# Keywords that indicate a transport request
TRANSPORT_KEYWORDS = {"cab", "taxi", "transport", "airport", "transfer", "car", "ride", "pickup", "drop"}

# Keywords that suggest intercity travel
INTERCITY_KEYWORDS = {"intercity", "outstation", "city to city", "another city", "other city", "highway"}

# Common Indian city names for intercity detection
CITY_NAMES = {
    "mumbai", "delhi", "bangalore", "bengaluru", "chennai", "hyderabad",
    "kolkata", "pune", "jaipur", "ahmedabad", "lucknow", "goa",
    "kochi", "chandigarh", "agra", "varanasi", "udaipur", "mysore",
    "mysuru", "shimla", "manali", "ooty", "coorg", "pondicherry",
}

TIME_PATTERN = re.compile(
    r"\b((?:[01]?\d|2[0-3]):[0-5]\d(?:\s*(?:am|pm))?|(?:[1-9]|1[0-2])\s*(?:am|pm)|(?:[01]?\d|2[0-3])\s*(?:hrs|hours))\b",
    re.IGNORECASE,
)
TERMINAL_PATTERN = re.compile(r"\b(?:terminal|t)\s*([12])\b", re.IGNORECASE)


class TransportHandler(BaseHandler):
    """Handles cab, taxi, airport transfer, and intercity transport requests."""

    async def handle(
        self,
        message: str,
        intent_result: IntentResult,
        context: ConversationContext,
        capabilities: dict[str, Any],
        db_session: Any = None,
    ) -> HandlerResult:
        msg_lower = message.lower()

        if context.pending_action == "collect_transport_details":
            return self._handle_transport_details(message, context)

        # --- Capability check ---
        if not self._is_capability_enabled(capabilities, "transport"):
            return HandlerResult(
                response_text=(
                    "I'm sorry, transport services are not available at this time. "
                    "Please contact the front desk for assistance with travel arrangements."
                ),
                next_state=ConversationState.IDLE,
                suggested_actions=["Contact reception", "Need help"],
            )

        # --- Detect transport type ---
        is_intercity = self._is_intercity_request(msg_lower)
        is_airport = self._is_airport_request(msg_lower)

        # --- Intercity check ---
        if is_intercity:
            city = self._get_business_city(capabilities) or "the city"
            # Check if intercity capability is explicitly available
            # (transport config may have sub-capabilities; for now use a simple check)
            transport_caps = capabilities.get("capabilities", {}).get("transport", {})
            intercity_enabled = transport_caps.get("intercity", False)

            if not intercity_enabled:
                return HandlerResult(
                    response_text=(
                        f"We only provide local cab services within {city}. "
                        "For intercity travel, we recommend external providers "
                        "such as ride-hailing apps or our concierge can assist "
                        "with recommendations."
                    ),
                    next_state=ConversationState.IDLE,
                    suggested_actions=["Local cab", "Airport transfer", "Talk to concierge"],
                )

            return HandlerResult(
                response_text=(
                    "I'll arrange an intercity cab for you. "
                    "Our team will confirm the vehicle, fare, and pickup details shortly."
                ),
                next_state=ConversationState.IDLE,
                metadata={"transport_type": "intercity"},
            )

        # --- Airport transfer ---
        if is_airport:
            return HandlerResult(
                response_text=(
                    "I'll arrange an airport transfer for you. "
                    "Our team will confirm the vehicle and pickup details shortly. "
                    "Could you share your flight time so we can plan accordingly?"
                ),
                next_state=ConversationState.AWAITING_INFO,
                pending_action="collect_transport_details",
                pending_data={
                    "transport_type": "airport_transfer",
                    "requested_message": message.strip()[:240],
                },
                suggested_actions=["Flight at 12 PM, Terminal 2", "Flight at 6:30 AM, Terminal 1"],
                metadata={"transport_type": "airport_transfer"},
            )

        # --- Default: local cab ---
        return HandlerResult(
            response_text=(
                "I'll arrange a local cab for you. "
                "Our team will confirm the details shortly. "
                "Is there anything else you need?"
            ),
            next_state=ConversationState.IDLE,
            metadata={"transport_type": "local"},
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _is_intercity_request(self, msg_lower: str) -> bool:
        """Check if the message is requesting intercity travel."""
        # Explicit intercity keywords
        for keyword in INTERCITY_KEYWORDS:
            if keyword in msg_lower:
                return True

        # Check if two different city names appear in the message
        found_cities = [city for city in CITY_NAMES if city in msg_lower]
        if len(found_cities) >= 2:
            return True

        # Check for pattern like "to <city>" when business city is different
        for city in CITY_NAMES:
            if re.search(rf"\bto\s+{city}\b", msg_lower):
                return True

        return False

    def _is_airport_request(self, msg_lower: str) -> bool:
        """Check if the message is about an airport transfer."""
        airport_terms = {"airport", "flight", "terminal", "departure", "arrival"}
        return any(term in msg_lower for term in airport_terms)

    def _handle_transport_details(self, message: str, context: ConversationContext) -> HandlerResult:
        """Handle follow-up details after transport prompt."""
        pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        transport_type = str(pending.get("transport_type") or "transport").strip()

        flight_time = self._extract_time(message)
        terminal = self._extract_terminal(message)

        details: dict[str, Any] = {
            "transport_type": transport_type,
            "details_message": message.strip()[:240],
        }
        if flight_time:
            details["flight_time"] = flight_time
        if terminal:
            details["terminal"] = terminal

        if not flight_time:
            return HandlerResult(
                response_text=(
                    "Thanks. To complete this request, please share your flight time "
                    "(for example: 12 PM or 18:30)."
                ),
                next_state=ConversationState.AWAITING_INFO,
                pending_action="collect_transport_details",
                pending_data=details,
                suggested_actions=["12 PM", "6:30 AM"],
                metadata={"transport_type": transport_type, "details_collected": False},
            )

        terminal_text = f", {terminal}" if terminal else ""
        return HandlerResult(
            response_text=(
                f"Got it. I've noted your {transport_type.replace('_', ' ')} request at {flight_time}{terminal_text}. "
                "Our team will confirm the pickup details shortly."
            ),
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            suggested_actions=["Need anything else?", "Talk to concierge"],
            metadata={"transport_type": transport_type, "details_collected": True, **details},
        )

    @staticmethod
    def _extract_time(message: str) -> str:
        match = TIME_PATTERN.search(message or "")
        if not match:
            return ""
        return match.group(1).strip().lower().replace("  ", " ")

    @staticmethod
    def _extract_terminal(message: str) -> str:
        match = TERMINAL_PATTERN.search(message or "")
        if not match:
            return ""
        return f"Terminal {match.group(1)}"
