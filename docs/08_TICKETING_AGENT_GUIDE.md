# Ticketing Agent Guide 

Last updated: 2026-02-26

## 1) What This Is

The Ticketing Agent is the part of your bot that turns guest requests into actionable support tickets for hotel teams.

Its goal is simple:
- catch requests that need staff action,
- avoid duplicate tickets,
- keep guests informed clearly,
- route each case to the right team quickly.

---

## 2) What Guests Experience

From a guest point of view, the flow feels like this:

1. Guest asks for help.
2. Bot understands whether this needs staff action.
3. Bot asks for missing details only when needed (for example room number or timing).
4. Bot confirms or proceeds based on context.
5. Bot creates a new ticket, updates an existing one, or acknowledges an already-open one.
6. Guest gets a clear response that the team is handling it.

The guest should not feel like they are "using a ticket system". It should feel like smooth service support.

---

## 3) Full Flow (End to End)

### Step A: Request Detection
The bot checks if the message is operational and needs human/staff action, not just information.

### Step B: Case Matching
It matches the request to configured ticketing cases (the business-defined rules visible in Admin UI).

### Step C: Detail Collection
If details are missing, the bot asks short follow-up questions before creating a ticket.

### Step D: Duplicate Protection
Before creating a new ticket, the bot checks if this is:
- already reported (acknowledge), or
- same issue with new detail (update), or
- truly new (create).

### Step E: Ticket Action
The bot then performs one action:
- Acknowledge existing open issue,
- Update existing issue with new notes, or
- Create new ticket.

### Step F: Guest Confirmation Message
Guest receives a clean status message (for example, team informed, ticket raised, or update added).

---

## 4) Cases Currently Covered

Your ticketing agent now includes these case groups:

1. Complaint or maintenance issue that needs staff action.
2. Human escalation or live agent support request.
3. Table booking support.
4. In-room dining food order support.
5. Spa booking support.
6. Room booking support.
7. Transport or pickup coordination.
8. Information not available in hotel data (fallback ticket).
9. Requested menu not available.
10. Pre-booking website, payment, or quotation issue.
11. Booking update, modify, or follow-up request.
12. Sightseeing directions unavailable or unclear (Guest Relations follow-up).
13. Generic booking-help request.
14. Special immediate requests (example: birthday cake, shaving gel).

---

## 5) Smart Behaviors

### No Duplicate Spam
If a guest repeats the same issue, the bot avoids opening another fresh ticket.

### Update Instead of Recreate
If guest adds new details to an open issue, the bot updates that issue.

### Priority Awareness
Serious or urgent complaints are treated with higher urgency.

### Team-Oriented Routing
The bot tries to map requests to the right service team for faster handling.

### Friendly Service Tone
Even when collecting details, the bot stays service-first and guest-friendly.

---

## 6) What Hotel Teams Gain

- Cleaner ticket quality (better issue summaries).
- Fewer duplicates.
- Better visibility of what guest asked and when.
- Better continuity between bot conversation and staff follow-up.
- Faster first response for common service requests.

---

## 7) What Admin Can Control

From Admin UI, you can:
- turn ticketing workflow on or off,
- add, remove, or edit ticketing cases,
- tune which requests should trigger ticket creation,
- review active ticketing case count in tools/workflows view.

This means operations teams can adjust behavior without changing code.

---

## 8) Success Criteria (Operational)

The ticketing agent is working well when:
- guests do not need to repeat the same issue,
- repeat messages mostly become updates, not duplicate tickets,
- staff receive clear, action-ready ticket summaries,
- escalation requests are captured reliably,
- unknown-information requests are safely converted to follow-up tickets.

---

## 9) Scope Note

This guide explains behavior and flow only.
It intentionally avoids technical internals, endpoints, and implementation details.

---

## 10) Remaining Lumira Parity Integrations

If the goal is to use the same output style as Lumira (tables, UI behavior, notifications), these are the key items still to align fully.

### A) Output Tables Parity (Same Lumira-style records)

1. Finalize full ticket output parity so every created/updated ticket carries the same operational fields used in Lumira reporting.
2. Keep parity on lifecycle fields used in operations:
- category
- sub-category
- priority
- phase
- status
- source
- SLA markers
- assignment markers
3. Align on the same operational ticket sink used by Lumira (`GHN_OPERATION_TICKETS`) for production parity.
4. Keep enrichment parity using Lumira reference mappings for guest, department, and outlet context.

### B) Operations UI / Dashboard Parity

1. Match Lumira-style ticket views for:
- open
- in progress
- closed
- canceled
- breached/overdue visibility
2. Match Lumira-style filtering and lookup experience:
- by guest
- by room
- by department
- by phase
- by source
3. Match ticket detail view expectations:
- issue summary
- manager notes
- assignment history
- latest update trail

### C) Notification Parity

1. New ticket notifications to relevant team/owner.
2. Existing ticket update notifications when new notes are added.
3. SLA warning and SLA breach alerts.
4. Escalation/handoff notifications to human support path.
5. Guest-facing acknowledgement consistency when ticket is created or updated.

### D) Channel Parity (Guest Journey + Engage)

1. Ensure same behavior for web widget and other channels where Lumira currently operates.
2. Keep source labeling parity (for example: manual, booking flow, channel-specific source labels).
3. Keep booking/pre-booking and during-stay ticket behavior consistent with Lumira experience.

### E) Handoff and Assignment Parity

1. Complete agent lookup and assignment behavior aligned with Lumira department mapping rules.
2. Ensure human handoff path uses the same routing expectations and follow-up ownership model.
3. Ensure ticket-plus-handoff flows behave consistently for urgent or repeated dissatisfaction cases.

---

## 11) Recommended Rollout Order for Lumira Parity

1. Output tables parity first (source of truth for reporting and operations).
2. Notification parity second (so teams do not miss new/updated issues).
3. UI/dashboard parity third (for day-to-day operations).
4. Full channel + handoff parity last (after data and alerts are stable).
