---
key: ticketing.expired_update_assessment
description: Decide whether a late follow-up note on an expired ticket should open a new human-support ticket
variables: [ticket_id, note_text, conversation]
---
You are deciding if a late follow-up should open a new human-support ticket.
Context: guest tried to update an old ticket after the update window.
Decide create_new_ticket=true only when the follow-up indicates a genuinely important change,
urgent unresolved impact, safety/security/health risk, or explicit need for human escalation.
If it is minor/noise/non-urgent repetition, set false.
Return strict JSON only:
{{"create_new_ticket": true|false, "priority": "low|medium|high|critical", "reason": "..."}}

Existing ticket id: {ticket_id}
Follow-up note: {note_text}
Conversation excerpt: {conversation}
