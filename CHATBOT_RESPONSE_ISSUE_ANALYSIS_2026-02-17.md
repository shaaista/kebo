# Chatbot Response Issue Analysis (Founder Demo Prep)

Date: 2026-02-17  
Source: Provided end-to-end chat transcript (web chat session)  
Scope: Functional response quality, RAG grounding behavior, and routing consistency  
Note: This document is analysis-only. No code behavior changes are included here.

---

## Executive Summary

The biggest demo-risk issue is **state lock-in from pending booking flow**.  
After the bot enters `select_restaurant`, many unrelated user questions are still answered as if the bot is waiting for restaurant input.

Second biggest issue is **domain gap**: users ask for workflows not yet modeled (room booking, outside recommendations, weather), and the bot maps them incorrectly into existing hospitality intents.

Third is **consistency and policy alignment**: service timings/capabilities are known, but downstream actions are not always validated against those constraints (example: booking at `2am` for a restaurant that closes at `22:00`).

---

## Detailed Issues

## 1) Sticky Pending Action Hijacks Unrelated Queries (Critical)

### Evidence
- User: `i want a room`  
  Bot: `Sure. Which restaurant would you like to book?`
- User then asks unrelated things (`weather`, `spa`, `outside restaurant`, `check in check out timing`) and repeatedly gets:
  `I couldn't match that restaurant...`

### Why this happens (core reason)
- Once conversation state/pending action is set to booking detail collection (`select_restaurant`), routing keeps prioritizing that pending flow.
- There is no robust interruption/escape condition for clearly unrelated intents.
- Result: unrelated turns are force-interpreted as booking continuation.

### Impact
- User feels bot is “stuck”.
- Demo quality drops sharply because basic FAQ/out-of-scope questions fail while in pending flow.

---

## 2) Room Booking Requests Are Misrouted to Table Booking (High)

### Evidence
- User: `i want a room`, `a room to sleep`, `book a room from feb 20-23`
- Bot moves into restaurant table booking prompts.

### Why this happens (core reason)
- There is no explicit `room_booking/stay_booking` workflow in enabled intent/capability set.
- Classifier is biased to nearest enabled hospitality intent, so “book room” is mapped to `table_booking`.

### Impact
- Fundamental intent correctness failure.
- For founder demo, this looks like the bot cannot differentiate core hotel journeys.

---

## 3) Generic DO Rule Over-Applies to Table Booking (High)

### Evidence
- User: `no chnage , reserve table for 2 at 2am`
- Bot asks: `To help accurately, could you share your location or room number?`

### Why this happens (core reason)
- NLU DO rule is generic: “Confirm timing, location, and count for service requests.”
- Runtime enforcement treats many requests similarly, including table booking, where room number/location is usually not mandatory.

### Impact
- Bot asks irrelevant details.
- Conversation feels robotic and policy-overconstrained.

---

## 4) Service Constraint Not Enforced at Action Time (High)

### Evidence
- Bot accepts reservation at `2am` for Kadak.
- Later tells user Kadak hours are `06:00 - 22:00`.
- User asks contradiction question; bot cannot resolve properly.

### Why this happens (core reason)
- Service hours are available in config and surfaced in info responses.
- Booking confirmation path does not validate requested booking time against outlet operating hours before confirmation.

### Impact
- Hard contradiction in a live demo.
- Shows gap between knowledge retrieval and transactional enforcement.

---

## 5) Mixed Source-of-Truth Creates Location/Fact Ambiguity (High)

### Evidence
- User: `which location is this` -> `Bangalore`
- Config has:
  - `city: bangalore`
  - `location/address: Mumbai`

### Why this happens (core reason)
- Multiple overlapping fields (`city`, `location`, `address`, KB text) are not conflict-resolved.
- Runtime answer may come from whichever source the current path favors.

### Impact
- Apparent factual inconsistency.
- Red flag for enterprise reliability.

---

## 6) Menu Request Handling Is Inconsistent for Equivalent Phrasing (Medium)

### Evidence
- `show menu` -> generic fallback safety message
- `i asked for menu` -> same generic fallback
- `food menu` -> full menu displayed correctly

### Why this happens (core reason)
- Different phrasing paths trigger different routing/validation outcomes.
- Some menu requests are likely going through a safety replacement path rather than deterministic menu handler output.

### Impact
- Perceived randomness.
- Users need to “guess the magic phrase”.

---

## 7) Recommendation Queries Underuse Available Menu Data (Medium)

### Evidence
- `recommend something nonveg` -> uncertain/human guidance
- `recommend someting spicy` -> escalates to human

### Why this happens (core reason)
- No dedicated recommendation workflow that uses structured menu metadata (`is_vegetarian`, category, etc.).
- These are treated as FAQ/general intents instead of a deterministic menu-recommendation pipeline.

### Impact
- Weak conversational intelligence despite having menu data.

---

## 8) Spelling Noise and Fuzzy Match Robustness Is Uneven (Medium)

### Evidence
- `restiarant`, `timigs`, `hcicken`, `poool pictures` produce inconsistent behavior.
- `i want hcicken pizza` not matched, no close suggestions provided.

### Why this happens (core reason)
- Input normalization/fuzzy matching/synonym handling is limited.
- Menu item matching is mostly direct lexical match, with no typo-tolerant candidate suggestion layer.

### Impact
- Real users with typing errors get poor outcomes.

---

## 9) Out-of-Scope Handling Is Not Stable During Active Flows (Medium)

### Evidence
- `what is the weather today` got forced into restaurant-selection response while pending booking was active.

### Why this happens (core reason)
- Out-of-scope or orthogonal intents are not allowed to interrupt/park pending transactional flows.

### Impact
- Bot appears context-blind and rigid.

---

## 10) Classification Telemetry Does Not Always Reflect Action Taken (Low/Observability)

### Evidence
- `5` shown as `unclear` with low confidence, but bot still correctly asks booking time.
- `10pm` shown as `unclear`, but bot composes correct booking confirmation.

### Why this happens (core reason)
- Deterministic pending-action routing can correctly handle message even when classifier confidence is low.
- UI telemetry surfaces raw classified intent/confidence, not “final routed handler”.

### Impact
- Stakeholder may misread quality from confidence labels alone.
- Analytics can look worse than user-visible behavior.

---

## 11) Data Hygiene Risk in Service Catalog (Medium)

### Evidence from current config
- Active services include expected outlets (`ird`, `kadak`), but there are extra inactive/duplicate-style entries:
  - `aviator` inactive
  - `cafe247` inactive
  - `id: restaurant`, `name: aviator`, inactive

### Why this happens (core reason)
- Historical/legacy entries and manual config updates can leave ambiguous rows.
- Runtime guardrails reduce some effects, but noisy catalog still increases routing/validation edge cases.

### Impact
- Higher chance of false matches and confusing behavior under edge phrasing.

---

## 12) Capability Gap: Missing Workflows for Founder Demo Expectations (High)

### Evidence
- User asks for:
  - Room booking
  - Outside restaurant recommendations
  - Weather
  - Pool pictures
- Bot has no strong modeled workflow for these in current config/runtime.

### Why this happens (core reason)
- Current architecture is strong for in-hotel operational intents (food/room service/booking/complaint/escalation), but not all “concierge” intents are fully implemented.

### Impact
- Demo may feel incomplete unless these are framed as planned scope.

---

## What To Highlight in Founder Demo (Without Changing Code Right Now)

1. Show strengths where system is strong now:
- In-hotel flows: booking, room service, complaint escalation.
- KB-grounded factual answers (check-in/out, spa, pool timings).
- Memory in transactional context (reservation details retrieval).

2. Avoid demo paths that currently expose known gaps:
- Room booking/stay booking.
- Outside-hotel recommendations + weather.
- Media requests (pool pictures).

3. Be transparent on why issues occurred:
- Most failures are **state/routing and workflow coverage gaps**, not random model hallucination.
- Data/source conflicts (city vs location/address) also caused factual inconsistency risk.

---

## Priority Root-Cause Buckets (for planning)

1. Conversation control:
- Pending-action interruption and intent preemption rules.

2. Domain modeling:
- Missing first-class intents/workflows (room booking, recommendations, external concierge asks).

3. Constraint enforcement:
- Action-time validation against service hours and capability windows.

4. Source-of-truth governance:
- Conflict detection and precedence across setup wizard fields vs KB.

5. Retrieval UX:
- Follow-up query rewriting and typo-tolerant matching for user phrasing variance.

