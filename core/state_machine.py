"""
Conversation State Machine

Manages state transitions and ensures valid conversation flow.
Prevents issues like asking for confirmation twice or losing context.
"""

from typing import Optional, Tuple

from schemas.chat import ConversationState, IntentType


# Valid state transitions
STATE_TRANSITIONS: dict[ConversationState, set[ConversationState]] = {
    ConversationState.IDLE: {
        ConversationState.AWAITING_CONFIRMATION,
        ConversationState.AWAITING_SELECTION,
        ConversationState.AWAITING_INFO,
        ConversationState.PROCESSING_ORDER,
        ConversationState.ESCALATED,
        ConversationState.COMPLETED,
        ConversationState.IDLE,  # Can stay idle for simple Q&A
    },
    ConversationState.AWAITING_CONFIRMATION: {
        ConversationState.IDLE,
        ConversationState.PROCESSING_ORDER,
        ConversationState.AWAITING_INFO,
        ConversationState.ESCALATED,
        ConversationState.COMPLETED,
    },
    ConversationState.AWAITING_SELECTION: {
        ConversationState.IDLE,
        ConversationState.AWAITING_CONFIRMATION,
        ConversationState.AWAITING_INFO,
        ConversationState.PROCESSING_ORDER,
        ConversationState.ESCALATED,
    },
    ConversationState.AWAITING_INFO: {
        ConversationState.IDLE,
        ConversationState.AWAITING_CONFIRMATION,
        ConversationState.PROCESSING_ORDER,
        ConversationState.ESCALATED,
        ConversationState.COMPLETED,
    },
    ConversationState.PROCESSING_ORDER: {
        ConversationState.IDLE,
        ConversationState.AWAITING_CONFIRMATION,
        ConversationState.COMPLETED,
        ConversationState.ESCALATED,
    },
    ConversationState.ESCALATED: {
        ConversationState.IDLE,  # After human resolves
        ConversationState.COMPLETED,
    },
    ConversationState.COMPLETED: {
        ConversationState.IDLE,  # New conversation starts
    },
}

# Intent to expected response state mapping
INTENT_EXPECTED_STATES: dict[IntentType, set[ConversationState]] = {
    IntentType.CONFIRMATION_YES: {
        ConversationState.AWAITING_CONFIRMATION,
    },
    IntentType.CONFIRMATION_NO: {
        ConversationState.AWAITING_CONFIRMATION,
    },
}


class StateMachine:
    """Manages conversation state transitions."""

    def can_transition(
        self,
        current_state: ConversationState,
        target_state: ConversationState,
    ) -> bool:
        """Check if a state transition is valid."""
        valid_targets = STATE_TRANSITIONS.get(current_state, set())
        return target_state in valid_targets

    def get_valid_transitions(
        self,
        current_state: ConversationState,
    ) -> set[ConversationState]:
        """Get all valid target states from current state."""
        return STATE_TRANSITIONS.get(current_state, set())

    def validate_intent_for_state(
        self,
        intent: IntentType,
        current_state: ConversationState,
    ) -> Tuple[bool, Optional[str]]:
        """
        Validate if an intent makes sense for the current state.

        Returns:
            (is_valid, error_message)
        """
        # Check if intent requires specific state
        expected_states = INTENT_EXPECTED_STATES.get(intent)

        if expected_states and current_state not in expected_states:
            return False, f"Received '{intent.value}' but not expecting confirmation"

        return True, None

    def determine_next_state(
        self,
        current_state: ConversationState,
        intent: IntentType,
        action_completed: bool = False,
        needs_confirmation: bool = False,
        needs_selection: bool = False,
        needs_info: bool = False,
        should_escalate: bool = False,
    ) -> ConversationState:
        """
        Determine the next state based on intent and action result.
        """
        # Escalation always takes priority
        if should_escalate or intent == IntentType.HUMAN_REQUEST:
            return ConversationState.ESCALATED

        # Confirmation intents
        if intent == IntentType.CONFIRMATION_YES:
            if current_state == ConversationState.AWAITING_CONFIRMATION:
                return ConversationState.PROCESSING_ORDER if not action_completed else ConversationState.COMPLETED
            return current_state  # Unexpected yes, stay in current state

        if intent == IntentType.CONFIRMATION_NO:
            return ConversationState.IDLE  # Cancel and return to idle

        # Determine based on what's needed next
        if needs_confirmation:
            return ConversationState.AWAITING_CONFIRMATION
        if needs_selection:
            return ConversationState.AWAITING_SELECTION
        if needs_info:
            return ConversationState.AWAITING_INFO
        if action_completed:
            return ConversationState.COMPLETED

        return ConversationState.IDLE

    def get_state_context_hint(self, state: ConversationState) -> str:
        """Get a hint about what the bot should remember in this state."""
        hints = {
            ConversationState.IDLE: "Ready for new request",
            ConversationState.AWAITING_CONFIRMATION: "User must confirm or deny pending action",
            ConversationState.AWAITING_SELECTION: "User must select from presented options",
            ConversationState.AWAITING_INFO: "User must provide specific information",
            ConversationState.PROCESSING_ORDER: "Order is being processed",
            ConversationState.ESCALATED: "Conversation handed to human agent",
            ConversationState.COMPLETED: "Task completed, ready for new request",
        }
        return hints.get(state, "Unknown state")


# Global instance
state_machine = StateMachine()
