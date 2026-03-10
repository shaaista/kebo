# Pre-Booking Conversation Fault Audit (2026-03-10)

## Scope
- Source conversation: your shared pre-booking transcript.
- Matching runtime session found in logs: `session_1773149430074_ydab9zl3w`.
- Evidence sources used:
  - `logs/conversation_audit.jsonl`
  - `handlers/complaint_handler.py`
  - `services/chat_service.py`
  - `services/ticketing_service.py`
  - `static/js/chat.js`
- Code changes: **none** (analysis only).

## Faults Found (With Reasons and Suggestions)

### 1) Website booking-link complaint asks for room number (wrong required field)
- Evidence:
  - `logs/conversation_audit.jsonl:821` bot asks room number for website booking-link issue.
- Why this happens:
  - Complaint ticket flow defaults phase to in-stay when no explicit phase entity is present:
    - `handlers/complaint_handler.py:182` (`"phase": ... or "in_stay"`)
  - Room is required unless pre-booking/pre-checkin is detected:
    - `handlers/complaint_handler.py:1480-1511`
  - So digital pre-booking issues can be treated like room ops.
- Generalized suggestion:
  - Replace one-size room prompt with **issue-domain-based required details**.
  - For each complaint domain (digital/booking-flow, billing, in-room maintenance, etc.), infer required slots from context and phase.

### 2) Ticket payload for pre-booking web issue is semantically wrong for dashboard routing
- Evidence:
  - `logs/conversation_audit.jsonl:822` ticket `5477` response includes:
    - `"phase": "During Stay"`
    - `"orgId": 0`
    - `"groupId": null`, `"messageId": null`
- Why this happens:
  - Same phase default issue (`in_stay`) propagates into payload.
  - Web UI sends only phase metadata:
    - `static/js/chat.js:139-141`
  - No entity/group/message IDs are supplied from the widget path.
  - Integration fallback sets organisation from hotel code (often `DEFAULT`):
    - `services/chat_service.py:14475`
- Generalized suggestion:
  - Enforce a dashboard-mode metadata contract (`entity_id`, `group_id`, `message_id`, valid org mapping).
  - If missing, either enrich from trusted source before create, or mark as non-routable and ask for valid identity context.

### 3) Booking confirmation turn misclassified as complaint; no new ticket on confirm
- Evidence:
  - `logs/conversation_audit.jsonl:836`
  - User `yes confirm`, bot confirms booking, but intent is `complaint` and `ticket_created=false`.
  - Metadata shows `ticket_route_decision=acknowledge`, `ticket_candidates_checked=1`.
- Why this happens:
  - Intent/router drift in full-KB path causes complaint fallback routing.
  - Existing-ticket smart routing acknowledges prior ticket instead of creating a transaction-specific one.
- Generalized suggestion:
  - Use **pending_action-first** handling for confirmation turns.
  - Use transaction-specific dedupe keys (operation+category+reference), not broad similarity to any open ticket.

### 4) Duplicated phrasing in response
- Evidence:
  - `logs/conversation_audit.jsonl:831`
  - Response: `"I can proceed with I can proceed with Prestige Suite..."`
- Why this happens:
  - A generated phrase is stored as slot value and then wrapped by another template.
- Generalized suggestion:
  - Canonicalize extracted slot values to normalized labels before templating (e.g., room type dictionary mapping).

### 5) Phase messaging adds unrelated extra service lines
- Evidence:
  - `logs/conversation_audit.jsonl:832` (spa/pool query; response also adds restaurant reservation unavailability).
- Why this happens:
  - Additional unavailable-service lines are appended from broad intent inference:
    - `services/chat_service.py:12625-12691`, `13048-13223`
- Generalized suggestion:
  - Limit extra phase lines to high-confidence secondary matches, and keep direct ask as primary.

### 6) Room-focused memory bleeds into unrelated extraction
- Evidence:
  - `logs/conversation_audit.jsonl:827-831`
  - `deterministic_room_updates.room_type_candidates` includes irrelevant phrases from older complaint context.
- Why this happens:
  - Extraction considers broader conversation text without strict topical filtering by active flow.
- Generalized suggestion:
  - Scope extraction window to current active flow (`pending_action`/intent cluster), with decay for unrelated turns.

### 7) Dashboard visibility issue is likely identity/scope mismatch, not API failure
- Evidence:
  - API returned success and ticket IDs (`5477`, etc.) but with `orgId: 0` and missing routing IDs.
- Why this happens:
  - Missing end-to-end identity context from widget -> integration -> ticket payload.
- Generalized suggestion:
  - Verify dashboard user scope and payload org/entity mapping consistency before/at ticket creation.

## Additional Code-Level Risks Relevant to Your Requirements

### A) Hardcoded lexical markers still exist in operational issue detection
- Location:
  - `services/chat_service.py:6706-6763`
- Risk:
  - This conflicts with your “no hardcoded word-fix” requirement and can overfit behavior.
- Suggestion:
  - Shift to structured semantic classification using phase + service catalog + complaint-domain ontology.

### B) Booking handler ticket phase is fixed to during-stay in one path
- Location:
  - `handlers/booking_handler.py:428`
- Risk:
  - If that path is used, pre-booking booking confirmations can be stamped with wrong phase.
- Suggestion:
  - Resolve phase from context/pending/integration deterministically instead of fixed constant.

## Prioritized Suggestions

### P0 (Must fix for your current pain)
1. Phase-correct complaint intake and required-slot logic by issue domain.
2. Enforce valid identity metadata (`entity/group/message/org`) before dashboard ticket create.
3. Confirmation flow should prefer pending-action semantics over fresh intent classification.

### P1
1. Transaction-aware dedupe strategy for ticket creation.
2. Canonical slot normalization for room/service names.

### P2
1. Reduce extra phase-unavailable noise.
2. Tighten memory extraction window to active task context.

## User Additions (2026-03-10)

### 1) Real-time temporal sanity for all date/time actions
- Problem statement:
  - The assistant should reject actions that are already in the past.
  - Example: if today is **March 10, 2026**, booking/check-in requests for **March 2, 2026** should not proceed as valid.
- Generalized suggestion:
  - Add a common temporal validator for every transactional flow (room booking, transfer, table/spa booking, complaints with event dates).
  - Resolve dates in the property timezone first, then compare against current datetime.
  - If date is invalid/past, return a corrective response and ask for a valid alternative date.
  - Apply same logic to relative phrases (`today`, `tomorrow`, `next Monday`) and normalize to explicit dates internally.

### 2) Phase + stay-window + current-date consistency checks
- Problem statement:
  - Date requests should make sense with both the active phase and known stay window.
- Generalized suggestion:
  - Enforce:
    - no pre-booking actions for past arrival dates,
    - no during-stay service date outside known check-in/check-out,
    - no post-checkout service booking for in-stay-only services.
  - If check-in/check-out is missing, ask only for missing facts needed to validate dates.

### 3) Real-world context awareness (time/weather)
- Problem statement:
  - Bot responses for planning/travel should use live context when relevant.
- Generalized suggestion:
  - Add optional real-time context fetchers for:
    - current local time at property,
    - weather for requested date/location.
  - Use this only when it changes decision quality (travel timing, outdoor activities, transfer guidance), not for every turn.
  - If live context is unavailable, fail gracefully and state assumptions.

### 4) Ticket update window policy (tracking)
- Requirement:
  - Allow ticket update only for a short window after creation (2 minutes requested).
  - After window expiry, if follow-up is genuinely important, create a new human-followup ticket.
- Generalized suggestion:
  - Keep window duration configurable.
  - Use contextual urgency assessment (LLM + deterministic safety fallback) instead of hardcoded phrase rules.

## Pre-Checkin Retest Checklist (Suggested)

1. Pre-booking website issue:
- Input: `im not able to access booking link`
- Expected: asks contact/booking reference, **not room number** by default.

2. Same issue after user gives contact:
- Expected: ticket payload has correct phase (`Pre Booking`) and non-zero valid org/entity mapping.

3. Room booking confirm path:
- Input: booking details -> `yes confirm`
- Expected: consistent confirmation intent handling and deterministic ticket policy (create or explicit dedupe reason aligned to booking context).

4. Out-of-phase inquiry (`spa and pool` in pre-booking):
- Expected: concise explanation + availability phase mention without unrelated extra service noise.

5. Regression check for dashboard:
- Expected: created tickets visible under intended Kepsla scope/user filter.

6. Temporal sanity:
- Input: room booking/check-in date in the past (relative to current date).
- Expected: bot blocks invalid date and asks for a valid future date.

7. Relative-date normalization:
- Input: requests using `today`, `tomorrow`, `next Monday`.
- Expected: internally normalized correctly; behavior remains phase-consistent.

8. Expired ticket update:
- Input: update request after configured window.
- Expected: no direct update; bot asks for what changed, then either creates priority follow-up ticket (if important) or gives non-urgent guidance.

## User Additions Round 2 (Pre-Checkin + During-Stay) - 2026-03-10

### A) Pre-checkin airport transfer flow broke into complaint flow on confirmation
- Observed transcript:
  - Airport transfer details collected.
  - On `yes confirm`, bot switched to complaint flow and asked room number.
  - Then created complaint ticket unrelated to transfer.
- Symptom impact:
  - Wrong intent/handler handoff.
  - Random complaint ticket creation from a transfer workflow.
- Likely reason:
  - Confirmation turn is being re-routed by generic complaint/ticketing path instead of being bound to active transactional pending action.
  - Pending action/state isolation is not strict enough at confirmation turns.
- Generalized suggestion:
  - Enforce a strict confirmation router:
    - if `pending_action` is transactional (`confirm_transport`, `confirm_booking`, `confirm_order`, etc.), resolve confirmation only inside that flow.
    - complaint/ticketing interceptors must be bypassed until pending action is completed/cancelled.
  - Add a guard to prevent ticket category drift on confirmation replies.

### B) Bot got stuck asking room number after wrong complaint transition
- Observed transcript:
  - `what can u do for me` and `what services do u offer` were both treated as room-number replies.
- Symptom impact:
  - User cannot recover once flow is incorrectly switched.
- Likely reason:
  - Recovery/escape route from `collect_ticket_room_number` is weak.
- Generalized suggestion:
  - Add interruption handling for pending slots:
    - if user asks a meta/service question while a slot is pending, answer it and retain pending context, or offer explicit resume/cancel.
  - Add “exit pending flow” command handling (`cancel`, `start over`, `back`).

### C) Service-specific guidance is inconsistent; airport transfer asked “nonsense”
- Observed transcript:
  - `wjat details do u need` returned generic fallback instead of exact required fields.
- Symptom impact:
  - Hallucination-like behavior and poor user trust.
- Likely reason:
  - Service-specific required-slot prompts are not deterministic enough when user asks clarification.
- Generalized suggestion:
  - For every service, define a machine-readable intake schema:
    - required fields,
    - optional fields,
    - validation rules,
    - confirmation template.
  - On “what details do you need,” always render schema-driven required fields for current service flow.

### D) “Each service should have proper prompt” and auto-onboarding for new services
- Requirement:
  - Every service should have explicit behavior/information prompt.
  - New services should automatically get baseline prompts/intake behavior.
- Generalized suggestion:
  - Implement a service prompt factory from service config:
    - generate default prompt pack from service metadata (name, phase, ticketing flag, required details, policy constraints).
    - allow optional service-level override prompt.
  - Add config-time validation to fail if a service lacks required intake schema.

### E) Phase context appears mixed in memory; cross-phase bleed
- Observed transcript:
  - User notes that context appears to carry across phases in ways that confuse responses.
  - Example: modify room booking in pre-checkin asked target, then denied room booking in that phase (inconsistent journey handling).
- Generalized suggestion:
  - Store and retrieve facts with explicit phase scope:
    - `facts_by_phase[pre_booking|pre_checkin|during_stay|post_checkout]`.
  - Keep a global profile (guest identity/preferences) separate from phase-bound transactional facts.
  - When answering, prioritize current-phase memory, then global memory, then cross-phase fallback with explicit mention.

### F) Required information collection still incomplete across services
- Observed transcript:
  - During-stay complaint worked better, but other services still miss required data collection quality.
- Requirement:
  - Each service must collect complete required info before confirmation/ticketing.
- Generalized suggestion:
  - Use deterministic slot-completion check before confirm/create:
    - if required slots missing, ask only for missing ones.
  - Include slot confidence and allow correction turns (`change time`, `change guest count`, etc.).

### G) Reservation confirmation succeeded but intent reported as complaint
- Observed transcript:
  - Table booking was confirmed, but intent label showed `complaint`.
- Symptom impact:
  - Analytics, routing, ticketing, and memory quality degrade.
- Likely reason:
  - Intent relabeling after response generation or plugin path mismatch.
- Generalized suggestion:
  - Preserve two explicit fields:
    - `raw_detected_intent`,
    - `effective_handled_intent`.
  - Lock `effective_handled_intent` to handler that produced final response.

### H) View menu response gave generic phase services instead of menu content
- Observed transcript:
  - During-stay `View menu` did not return actual menu items.
- Generalized suggestion:
  - Add explicit menu intent branch with service-context binding:
    - if in dining flow or order intent active, fetch/show menu catalog directly.
  - Fallback to service list only if menu data is unavailable.

### I) Reservation ticket creation inconsistency
- Observed transcript:
  - User noted reservation ticket not created in expected path and hotel info capture gaps.
- Generalized suggestion:
  - Standardize ticketing policy per service action:
    - `always`, `conditional`, `never`.
  - For reservation flows marked ticket-enabled, emit explicit `ticket_created` or `ticket_skip_reason` with deterministic reasons.
  - Ensure hotel/property identifiers are mandatory for ticket-enabled actions.

## Added Retest Cases (Round 2)

9. Pre-checkin airport transfer confirmation integrity:
- Input: transfer request -> details -> `yes confirm`.
- Expected: stays in transfer flow, no complaint room-number prompt, no complaint ticket.

10. Pending-slot interruption recovery:
- Input during `collect_ticket_room_number`: `what services do u offer`.
- Expected: answer service question + preserve/offer resume for pending slot.

11. Service clarification:
- Input in active airport transfer flow: `what details do u need`.
- Expected: deterministic required field list, no generic fallback.

12. Table booking confirmation intent consistency:
- Input: `book table` -> details -> `yes confirm`.
- Expected: handled intent remains reservation/booking family, not complaint.

13. During-stay `View menu` behavior:
- Input: `order food` -> `view menu`.
- Expected: actual menu/catalog response for dining context.
