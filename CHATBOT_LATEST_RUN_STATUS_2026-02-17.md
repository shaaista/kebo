# Chatbot Latest Run Status (No Code Changes)

Date: 2026-02-17  
Source: Latest shared chat transcript (post-fix validation run)  
Scope: What is now working correctly vs mistakes still happening

---

## Working Properly

1. Greeting flow is stable.
- `hi` -> welcome response returned correctly.

2. Bot identity response works.
- `what is ur name` -> bot correctly says it is `kebo` for ICONIQA Demo Hotel.

3. Service-aware hotel booking info trigger works (first turn).
- `i wanna book hoel from 20-23` -> bot correctly identifies `Hotel Booking` service and gives availability + hours.

4. Menu request path works for at least one active outlet.
- `i want menu` and later `menu` -> structured menu returned (Kadak).

5. Room-delivery order flow works end-to-end (core transactional path).
- `deliver food to my room` -> asks for items.
- `pizza` -> order summary generated.
- `yes` -> order confirmed with order ID and delivery ETA.

---

## Mistakes Still Happening

## 1) Hotel Booking Follow-up Is Not Stateful

### Evidence
- User: `i wanna book hoel from 20-23`  
  Bot: hotel booking service info (correct)
- User follow-up: `20th feb to 23rd feb i want`  
  Bot: `Sure. Which restaurant would you like to book?...`

### Why this is still happening
- `hotel_booking` currently behaves as an informational service shortcut, not a full transactional workflow with its own pending-state collection.
- Follow-up date input is reclassified into existing booking flow (`table_booking`) because no dedicated hotel-stay booking handler/state exists.

---

## 2) Single-Character Confirmation Still Fails (`s`)

### Evidence
- After: `Would you like to see the full menu?`
- User: `s`
- Bot: clarification/unclear response

### Why this is still happening
- Confirmation normalization catches common yes/no variants, but a single-character `s` is too ambiguous and currently not treated as yes.

---

## 3) `in room dining menu` Returns Service Info, Not Menu

### Evidence
- User: `in room dining menu`
- Bot: `In-Room Dining is available. Multi-cuisine Hours...` (service info)

### Why this is still happening
- Service-information shortcut is matching first on service alias and answering metadata.
- It is still preempting menu rendering for some `service + menu` phrasing.

---

## 4) `in room menu` Falls to Generic Safe Response

### Evidence
- User: `in room menu`
- Bot: generic fallback (`I want to be accurate here...`)

### Why this is still happening
- Alias normalization for `in room` -> `in-room dining` is still inconsistent in this path.
- Query misses deterministic menu resolution and ends in fallback/validator replacement path.

---

## 5) Ambiguous Phrase `in room dining` Routes to Room Service

### Evidence
- User: `in room dining`
- Bot: asks room-service detail (`What do you need from room service...`)

### Why this is still happening
- Phrase is semantically ambiguous (could mean outlet/menu or service request).
- Current classification prioritizes `room_service` in this wording without disambiguation question.

---

## 6) Menu vs Order Catalog Still Looks Inconsistent

### Evidence
- Visible menu shown is Kadak-only (no pizza listed).
- User orders `pizza` and order succeeds (`Margherita Pizza`).

### Why this is still happening
- Menu display and order item lookup are likely not using exactly the same filtered dataset in all cases.
- This creates user-visible inconsistency: item can be ordered even when not visible in shown menu.

---

## Suggested Retest Checklist (Current Build)

1. Hotel booking continuity:
- `i wanna book hotel from 20 to 23`
- Follow with only date/time messages and confirm bot stays in hotel-booking journey.

2. In-room menu phrasing variants:
- `in room dining menu`
- `in room menu`
- `ird menu`
- Expect all to show actual menu items, not only service metadata.

3. Confirmation tolerance:
- After yes/no question, test `ys`, `y`, `s`, `ok`, `sure`.

4. Menu-order consistency:
- Ask menu, then order 3 items from that shown menu only.
- Verify no order is accepted for items absent from currently displayed active menu.

---

## Overall Status (This Run)

- Core basics: improved and stable (`greeting`, `identity`, `order confirmation`).
- Advanced continuity: still incomplete (`hotel stay booking flow`, `in-room menu routing`, `menu/order consistency`).
