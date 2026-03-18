# NexOria Hotel Chatbot — Full QA Test Report
**Date:** 2026-03-18
**Tester:** QA Agent (simulating real guest)
**Hotel:** Mumbai Grand Hotel (MUMBAI_GRAND / ICONIQA Hotel)
**Test Mode:** Chat Test Interface — all 4 lifecycle phases
**Tickets Observed:** #5577 (airport transfer), #5578 (in-room dining), #5579 (lost & found)

---

## Conversation Logs

### TEST 1 — Pre-booking (Phase: pre_booking)

| Turn | Role | Message |
|------|------|---------|
| 1 | Guest | Hi there! I've been looking for a good hotel in Mumbai for an upcoming trip. Can you tell me a bit about your property? |
| 2 | Bot | Welcome to ICONIQA Hotel in Mumbai! Located near Terminal 2, Andheri (E). 291 rooms: Premier King, Premier Twin, Lux Suites, Reserve Suites, Prestige Suites, Ultimate Suites. Amenities: smart laundry closet, complimentary minibar, high-speed WiFi, more. *(answered by: main orchestrator)* |
| 3 | Guest | What's the difference between the Lux Suite and the Prestige Suite? And how much do they cost per night? |
| 4 | Bot | Lux Suite: 380–485 sqft, King/Twin options, floor-to-ceiling windows, panoramic city views. Prestige Suite: 485 sqft, lavish king bed, bathtub. No pricing available — advised to contact reservations. *(answered by: main orchestrator)* |
| 5 | Guest | My wife and I are celebrating our 10th anniversary next month. Which room would you recommend and do you offer any special arrangements? |
| 6 | Bot | Recommended Prestige Suite (bathtub, king bed). Offered to assist with booking. *(answered by: room booking support agent)* |
| 7 | Guest | [clicked] Proceed with booking |
| 8 | Bot | Asked for: full name, phone number, check-in/check-out dates, number of guests. State → awaiting_info. *(answered by: room booking support agent)* |
| 9 | Guest | My name is Rahul Mehta, phone is 9820123456. Check-in April 15th, check-out April 18th, just the two of us. |
| 10 | Bot | Summary: Prestige Suite, 2 guests, April 15–18. Please confirm. *(answered by: room booking support agent)* |
| 11 | Guest | [clicked] Confirm Details |
| 12 | Bot | "I will now proceed with booking... please hold on." State → awaiting_confirmation. *(answered by: room booking support agent)* |
| 13 | Guest | [clicked] yes confirm |
| 14 | Bot | "Your booking is being processed." State → idle. **NO TICKET CREATED.** |

---

### TEST 2 — Pre-check-in (Phase: pre_checkin, Profile: guest_id 921346)

| Turn | Role | Message |
|------|------|---------|
| 1 | Guest | Hi, I have a booking arriving tomorrow. Can I arrange early check-in? Flight lands at 7am, hotel around 8:30am. |
| 2 | Bot | Standard check-in: 14:00. Early check-in at 50% room rate before 7am. For 8:30am arrival, check with front desk. "Booking for early check-in is not available in the current phase." *(answered by: main orchestrator)* |
| 3 | Guest | I also need an airport pickup. Flight IndiGo 6E-204, Terminal 2, 7:05am tomorrow. One person, one bag. |
| 4 | Bot | Routed to airport transfer agent. Asked for contact info. State → awaiting_info. *(answered by: airport transfer service agent)* |
| 5 | Guest | My phone is 9820123456, name is Rahul Mehta. |
| 6 | Bot | Airport pickup confirmed Terminal 2, 7:05am, ₹1500. Reference: #5577. State → idle. **TICKET CREATED #5577.** |
| 7 | Guest | Can you book a table for dinner at the hotel restaurant? 2 people, tonight around 8pm, anniversary setup. |
| 8 | Bot | "Restaurant table booking is not available in the current pre-checkin phase. Will be available during your stay." No ticket. |

---

### TEST 3 — During Stay (Phase: during_stay, Profile: guest_id 921348)

| Turn | Role | Message |
|------|------|---------|
| 1 | Guest | Hi, I'm in room 412. Can you send extra towels, change bed linen, need another pillow. |
| 2 | Bot | "Sending extra towels, changing bed linens, and providing pillows is not available through this service. Please contact housekeeping directly." No ticket. *(answered by: main orchestrator)* |
| 3 | Guest | Can I order room service? Chicken sandwich, tomato soup, two bottles of still water. Room 412. |
| 4 | Bot | Requested items not on menu. Offered: Veggie Sliders, Rainbow Veggie Pizza, Mac N Cheese, Red Sauce Spaghetti, Fries, Mashed Potato, Crispy Chicken Sliders. *(answered by: inroom dining agent)* |
| 5 | Guest | I'll go with Crispy Chicken Sliders and Mac N Cheese. And a Coke? |
| 6 | Bot | Coke not in menu. Confirmed Crispy Chicken Sliders + Mac N Cheese. Asked for room number and delivery time (room already stated). *(answered by: inroom dining agent)* |
| 7 | Guest | Room 412, deliver as soon as possible. |
| 8 | Bot | Order confirmed, delivering ASAP to room 412. Reference: #5578. **TICKET CREATED #5578.** *(answered by: inroom dining agent)* |
| 9 | Guest | Also, I'd like to book a couples massage at the spa for this evening, around 6pm. Both me and my wife. |
| 10 | Bot | "Booking a couples massage is not possible at the moment... booking services not available in the current phase." No ticket. *(answered by: main orchestrator)* |
| 11 | Guest | My wife is not feeling well, she has a fever and headache. Can you get a doctor to come to our room? |
| 12 | Bot | "We currently don't have a service to arrange a doctor visit. Contact front desk for medical needs." No ticket. No urgency escalation. *(answered by: main orchestrator)* |
| 13 | Guest | Can we do a late checkout tomorrow, around 2pm? |
| 14 | Bot | Policy provided (free until noon, 50% until 4pm, full charge after 4pm). "Booking not available in current phase — available during checkout phase." No ticket. *(answered by: main orchestrator)* |

---

### TEST 4 — Post-checkout (Phase: post_checkout, Profile: guest_id 921347)

| Turn | Role | Message |
|------|------|---------|
| 1 | Guest | Hi, I checked out this morning. Can I get a copy of my invoice/folio? Need it for expense claim. |
| 2 | Bot | "Cannot assist with invoice. Not available in post-checkout phase. Contact front desk/accounting." No ticket. *(answered by: main orchestrator)* |
| 3 | Guest | I left my phone charger in room 412. White iPhone charger. Can you check if it was found? |
| 4 | Bot | Routed to lost and found agent. Asked for checkout date and contact info. *(answered by: lost and found agent)* |
| 5 | Guest | Checked out March 18. Name: Rahul Mehta, phone 9820123456, email rahul.mehta@gmail.com. |
| 6 | Bot | Summarised details: white iPhone charger, room 412. Asked for confirmation. State → awaiting_confirmation. |
| 7 | Guest | [clicked] yes confirm |
| 8 | Bot | Ticket created. "Team will contact using details provided." Reference: #5579. **TICKET CREATED #5579.** *(answered by: lost and found agent)* |

---

## Phase-wise Analysis

### Phase 1 — Pre-booking

| Criterion | Result |
|-----------|--------|
| Hotel information accuracy | ✅ Correct — ICONIQA, 291 rooms, location |
| Room comparison (KB) | ✅ Accurate — Lux vs Prestige correctly differentiated |
| Pricing availability | ⚠️ Not in KB — correctly acknowledged gap |
| Special occasion handling | ✅ Routed to room booking agent, recommended Prestige Suite |
| Booking intent detection | ✅ Correctly identified and escalated to booking agent |
| Slot capture | ✅ All 5 slots captured (name, phone, room, dates, guests) |
| Booking year capture | ⚠️ RISK — "April 15th" not year-qualified |
| Ticket on confirmed booking | ❌ FAILED — No ticket after 3 confirmations |
| Double confirmation loop | ❌ ISSUE — "Confirm Details" + "yes confirm" creates confusing UX |

---

### Phase 2 — Pre-check-in

| Criterion | Result |
|-----------|--------|
| Early check-in query | ✅ Policy correctly stated; ⚠️ blocked for actual booking in pre-checkin phase |
| Airport transfer routing | ✅ Correctly routed to airport transfer service agent |
| Airport transfer slots | ✅ Flight, terminal, time captured |
| Ticket creation (#5577) | ✅ Created — correct category (transport/airport_transfer_service) |
| Contact info → ticket mapping | ❌ Phone captured in conversation; mobileNumber = null in ticket |
| Guest name in ticket | ❌ Profile name "Sairaj D" overrides conversation name "Rahul Mehta" |
| Restaurant reservation (pre-checkin) | ⚠️ Blocked by phase — potentially valid service missing |
| Premature action buttons | ⚠️ "yes confirm"/"cancel" shown before contact info collected |

---

### Phase 3 — During Stay

| Criterion | Result |
|-----------|--------|
| Housekeeping (towels/linen) | ❌ CRITICAL — Deflected. Core service not handled |
| Room service routing | ✅ Correctly routed to inroom dining agent |
| Menu KB accuracy | ✅ Correctly identified unavailable items; listed valid alternatives |
| Room service ticket (#5578) | ✅ Created — correct category (in-room_dining/food_order) |
| Room number re-ask | ⚠️ Room 412 provided upfront but asked again mid-flow |
| Spa booking (during stay) | ❌ CRITICAL — Blocked by phase gate incorrectly |
| Doctor on call | ❌ CRITICAL — Rejected entirely; no escalation for medical urgency |
| Late checkout | ⚠️ Policy provided but booking blocked by phase |
| Multiple service handling | ⚠️ Handled sequentially, not concurrently |
| Context between service turns | ⚠️ Room number not retained; context drift in suggested actions post-doctor |

---

### Phase 4 — Post-checkout

| Criterion | Result |
|-----------|--------|
| Invoice/folio request | ❌ CRITICAL — Rejected in post-checkout phase; core service |
| Lost item routing | ✅ Correctly routed to lost and found agent |
| Lost item slot collection | ✅ Item description, room, date, contact captured |
| Lost item ticket (#5579) | ✅ Created — correct category (lost_and_found/electronics) |
| Email/phone in ticket fields | ❌ emailId = null, mobileNumber = null despite being provided |
| Guest name mapping | ❌ Profile name "ChaitraNag2" overrides "Rahul Mehta" |

---

## Orchestration Evaluation

**Routing quality:**
- Airport transfer → airport transfer service agent ✅
- Room service → inroom dining agent ✅
- Anniversary booking → room booking support agent ✅
- Lost and found → lost and found agent ✅
- Housekeeping → main orchestrator ❌ (no housekeeping agent found)
- Spa booking → main orchestrator ❌ (not routed to spa agent)
- Doctor on call → main orchestrator ❌ (not routed to medical/emergency agent)
- Invoice → main orchestrator ❌ (not routed to billing agent)

**Multi-service within session:** Services handled sequentially without cross-contamination of contexts. However, the ticket message log accumulates the full conversation history including failed/unrelated turns — context pollution risk for operations teams.

**Phase transitions:** Manual phase switching works. Profiles auto-load correctly per phase. However, the phase-gate logic appears overly restrictive, blocking valid services during active phases.

---

## Ticketing Accuracy

| Ticket | Service | Triggered | Category | Correct |
|--------|---------|-----------|----------|---------|
| Pre-booking booking confirmation | Room Booking | ❌ No | — | Under-trigger |
| #5577 | Airport Transfer | ✅ Yes | transport/airport_transfer_service | ✅ |
| Housekeeping | Towels/Linen | ❌ No | — | Under-trigger |
| Spa | Couples Massage | ❌ No | — | Under-trigger (phase blocked incorrectly) |
| Doctor on Call | Medical Emergency | ❌ No | — | Under-trigger (critical) |
| #5578 | In-room Dining | ✅ Yes | in-room_dining/food_order | ✅ |
| #5579 | Lost & Found | ✅ Yes | lost_and_found/electronics | ✅ |
| Invoice/Folio | Billing | ❌ No | — | Under-trigger |

**Summary:** 3 of 8 tested services triggered tickets correctly. 5 under-triggers identified. No over-triggers detected.

---

## Context Management Evaluation

| Aspect | Result |
|--------|--------|
| Room number retention (412) | ❌ PARTIAL — provided upfront, re-asked mid-session |
| Guest name retention | ✅ Used within conversation flow; ❌ not mapped to ticket |
| Conversation history in ticket | ✅ Full conversation context included (informative but verbose) |
| Phase context across turns | ✅ Consistent within session |
| Suggested actions relevance | ⚠️ Post-doctor suggested actions reverted to dining (irrelevant) |
| Service agent switching | ✅ Clean handoffs between agents within session |

---

## Key Issues & Gaps

### CRITICAL (Blocking)
1. **Booking ticket under-trigger** — Confirmed room booking (3 confirmations) produces no ticket. The booking flow completes conversationally but is not operationally captured.
2. **Doctor on call not handled** — Medical emergencies deflected to front desk. No ticket, no urgency escalation. Safety risk.
3. **Housekeeping not handled** — Towels, linen, pillows — fundamental in-stay services rejected.
4. **Invoice/folio blocked post-checkout** — Most common post-stay request rejected. Billing team receives no ticket.

### HIGH (Service Quality)
5. **Spa booking blocked during-stay** — Phase gate incorrectly blocks spa during the active stay phase.
6. **Guest identity not mapped to tickets** — `guestName`, `mobileNumber`, `emailId` in ticket payload always come from the pre-loaded profile, never from conversation-captured data. Operational teams receive wrong contact information.
7. **Double confirmation loop** — Booking requires "Confirm Details" then "yes confirm" — redundant and confusing.

### MEDIUM (UX & Data Quality)
8. **Premature action buttons** — "yes confirm" / "cancel" shown before required info is actually collected.
9. **Room number re-asked** — Guest stated room 412 at turn 1; bot re-asks at turn 6. Sub-turn context loss.
10. **Booking year not captured** — "April 15th" accepted without year; ambiguous for reservation systems.
11. **Ticket message log pollution** — Full conversation history in ticket messages includes unrelated prior turns (e.g., failed housekeeping request appears in the room service ticket log).
12. **Late checkout blocked during stay** — Should be requestable before checkout phase.
13. **Restaurant reservation blocked pre-checkin** — Pre-arrival dining reservations are a standard hotel service.
14. **KB gaps** — Chicken sandwich, soup, water, Coke not in in-room dining KB. Very basic items missing.

### LOW (Observations)
15. **Response speed** — Average 10–15 seconds per response (observed via waiting patterns). May impact real-time WhatsApp/chat UX.
16. **Suggested actions drift** — Post-medical-request suggested actions pivoted to dining (unrelated context).
17. **Early check-in policy** — Bot states 7am threshold but guest arriving at 8:30am; policy application guidance is good but booking blocked.

---

## Overall System Readiness Assessment

| Dimension | Score | Notes |
|-----------|-------|-------|
| Service detection accuracy | 5/10 | Good for working services; completely misses housekeeping, spa, doctor, billing |
| Service routing/mapping | 6/10 | Airport, dining, lost&found route correctly; 4 major services have no agent |
| Ticketing accuracy | 4/10 | 3 of 8 services ticketed; booking confirmation failure is most critical |
| Slot capture | 7/10 | Good capture in conversation; poor mapping to ticket payload fields |
| Context retention | 5/10 | Within-agent good; cross-turn room number loss; ticket payload mapping fails |
| Phase transition logic | 5/10 | Auto-profile loading works; phase gates too restrictive for core services |
| Orchestration quality | 6/10 | Multi-service sequential handling clean; routing gaps on key services |
| Guest experience | 5/10 | Good conversational flow for working paths; frustrating for blocked services |
| **Overall Readiness** | **5/10** | **Not production-ready. Critical gaps in housekeeping, medical, booking confirmation, and billing must be resolved before live deployment.** |

---

*Report generated: 2026-03-18 | QA tested by: Automated QA Agent acting as real guest*
