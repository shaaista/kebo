# Chatbot Error Report (Run 2)

Date: 2026-02-17  
Scope: Analysis of the latest shared chat transcript only (no code changes)

---

## Summary

Core issue in this run: the bot still maps **room-stay booking language** to the **restaurant table-booking flow**.  
Secondary issue: menu phrasing behavior is still inconsistent (`in room menu` fails, but `menu for in room dining` works).

---

## What Worked

1. Greeting is correct.
- `hi` -> proper welcome response.

2. Identity response is correct.
- `who are you` -> bot correctly identifies itself.

3. Full in-room menu rendering works when phrasing is explicit.
- `menu for in room dining` -> correct itemized In-Room Dining menu.

4. Order flow from item mention to summary works.
- `pasta only for me right now` -> proper order summary + confirmation prompt.

5. Cancellation flow works.
- `No, cancel` -> bot cancels cleanly.

---

## Errors and Reasons

## E01 - Room-Stay Request Misrouted to Restaurant Booking

### Evidence
- User: `i want a room from feb 20-23`
- Bot: `Sure. Which restaurant would you like to book? We currently have: Kadak, in room dining.`

### Why this is wrong
- User asked for hotel stay intent (room booking context), not restaurant reservation.
- Even if room booking is not implemented yet, bot should return a capability-unavailable response, not wrong domain routing.

### Core reason
- Intent mapping still overweights booking keywords and falls into existing `table_booking` flow.
- Missing negative guard like: if message indicates stay/room-date booking and no stay workflow enabled, block table-booking handoff.

---

## E02 - Equivalent Menu Phrase Still Fails (`in room menu`)

### Evidence
- User: `in room menu`
- Bot: generic safe fallback (`I want to be accurate here...`)
- But next:
- User: `menu for in room dining`
- Bot: correct In-Room Dining menu.

### Why this is wrong
- Both messages mean the same user intent.
- Behavior should be deterministic across common phrasings.

### Core reason
- Alias/phrase normalization is still uneven for short service+menu utterances.
- One path reaches deterministic menu handler; another falls into validator/fallback response.

---

## E03 - Order Detail Prompt Is Generic/Policy-Driven Instead of Item-Driven

### Evidence
- User: `send over pasta to my room`
- Bot: `To help accurately, could you share your preferred time, the number of people/items?`

### Why this is wrong
- User already gave a clear actionable request (`pasta`, `to my room`).
- Better next step should be item clarification only (which pasta / quantity), not generic “time + people/items”.

### Core reason
- Policy/validator DO-rule replacement is too generic for partially parsed order intents.
- Runtime does not prioritize extracted menu entities strongly enough before applying generic request-template prompts.

---

## E04 - Classification Label Still Misleading for Stakeholder View

### Evidence
- The misrouted room request shows as `table_booking` with moderate confidence.

### Why this is wrong
- The confidence display can make a wrong route look “acceptable.”

### Core reason
- UI telemetry shows classifier label/confidence, but not enough signal of domain mismatch or route override risk.

---

## Expected Behavior for Room-Stay Queries (Current Scope)

For messages like `i want a room from feb 20-23`, the bot should respond with:

1. Acknowledge intent.
2. State that room-stay booking is not enabled in current assistant scope.
3. Offer valid alternatives (front desk/human handoff/available supported services).

It should **not** enter restaurant selection.

---

## Root-Cause Buckets (This Run)

1. Intent boundary enforcement gap.
- Missing hard separation between stay-booking language and restaurant-booking flow.

2. Phrase normalization gap.
- Equivalent menu phrasings do not consistently route to menu handler.

3. Over-generic policy rewrite.
- Generic DO-rule prompts can override more specific order-collection prompts.

4. Observability gap.
- Reported intent/confidence can hide domain mismatch in actual behavior.

