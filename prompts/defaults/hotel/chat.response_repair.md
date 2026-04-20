---
key: chat.response_repair
description: User-message body used when asking the LLM to repair a reply that failed validation
variables: [user_message, intent_value, repair_issue_text, phase_label, phase_id, phase_services_text, capability_check_allowed, capability_reason, base_text, candidate, safe_text]
---
Rewrite the assistant response so it passes runtime policy checks.
Return plain text only.
Do not use templated/canned phrasing.
Keep meaning aligned with policy restrictions and available services.
Do not promise blocked actions.

PRIMARY vs STEADY-STATE KNOWLEDGE:
If a block labeled "PRIMARY KNOWLEDGE (hotel-specific, current, always authoritative)" is present in your context, apply these rules when rewriting:
1. PRIMARY is the current truth. If PRIMARY and STEADY-STATE disagree, the repaired reply states PRIMARY and uses STEADY-STATE only as background ("usually X, but currently Y").
2. Preserve any date window or reopen date from PRIMARY (e.g. "closed until Apr 25"). Never silently drop it.
3. Never repair by falling back to STEADY-STATE facts that PRIMARY has overridden.
4. Never entertain hypothetical bypasses of the current PRIMARY reality.

User message: {user_message}
Intent: {intent_value}
Policy issue codes: {repair_issue_text}
Current phase: {phase_label} ({phase_id})
Current phase services: {phase_services_text}
Capability allowed: {capability_check_allowed}
Capability reason (if blocked): {capability_reason}

Original draft:
{base_text}

Unsafe candidate:
{candidate}

Validator-safe fallback reference:
{safe_text}
