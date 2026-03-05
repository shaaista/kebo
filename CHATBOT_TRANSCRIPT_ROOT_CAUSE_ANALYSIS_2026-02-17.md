# Chatbot Transcript Root-Cause Analysis

Date: 2026-02-17  
Input: User-provided end-to-end chat transcript  
Scope: Root cause + practical fix plan

## Executive Summary

The run shows the bot is functional for basic intents, but several failures come from one combined pattern:

1. Runtime service catalog is effectively empty in booking flows.
2. Booking prompts are being overwritten by generic validator text.
3. Delivery/order messages are sometimes classified into `room_service`.
4. Order matching is item-only (weak category + typo tolerance).
5. Profile updates (room number) are captured in memory but not acknowledged as context updates.

These are fixable without architecture rewrite. Most are handler/routing guardrail updates.

## Findings

| ID | Symptom In Transcript | Root Cause | Fix |
|---|---|---|---|
| F1 | First `hi` fails with generic error, second `hi` works | First request can fail due startup/DB connection timing and route-level exception (`/api/chat/message`) | Add request-time DB fallback/retry and better 503 payload; avoid generic frontend-only error |
| F2 | `Which service would you like to book? Available options: our available services.` | `service_catalog` empty (`config/business_config.json` has `"services": []`), plus DB augmentation is gated off when `menu_runtime_enabled=false` | Populate services in config or always merge active DB services for booking/order flows |
| F3 | `what services are availabke` returns KB no-match | Service discovery fell into FAQ/RAG path with no service-catalog fallback | Add deterministic service-list response before RAG for service-discovery queries |
| F4 | `book table at kadak` then `2am 5 guests` fails with "couldn't match that service" | Booking state likely stuck at `select_service`; validator replaced handler prompt with generic detail prompt, confusing user flow | Do not allow validator replacement when handler is collecting service slot; preserve slot-specific prompt |
| F5 | `deliver chicken biryani from kadak to room 402` routed to `room_service` | Classifier/rules overweight "room" phrase and miss strong food-order signal | Add deterministic pre-classification rule: delivery/order + food item/outlet => `order_food` |
| F6 | `i want some snacks` / `i want a drink wine` fails item lookup | Order handler matches item names only; no category-level fallback | Add category mapping (`snacks`, `beverages`, `desserts`) + show top matched items |
| F7 | `i want chicjen pizza to my room` fails | Fuzzy matching is single-string and threshold-based, weak for typo+multi-token phrases | Token-level fuzzy + n-best suggestions in response |
| F8 | `my room number is 202` gives irrelevant FAQ-like answer | Room number gets extracted into memory facts but not promoted to active context/ack flow | Update `context.room_number` from extracted facts and return deterministic acknowledgment |
| F9 | Intent shown as `table_booking` even when bot denies room-stay booking | UI displays classifier label, not final routed action/capability-denial reason | Return and display `response_source` + `final_action` in UI metadata |
| F10 | Menu answer includes `details incomplete`; policy answer quality inconsistent | LLM-formatted RAG response not fully constrained by post-processing; no strict policy extractor | Add post-filter for placeholder artifacts + deterministic policy parser for check-in/out and charges |

## Code-Level Root Causes

1. Empty service runtime context for booking
- `config/business_config.json`: `"services": []`
- `services/config_service.py`: `get_capability_summary()` returns `"restaurants": []` and builds `service_catalog` from config services only.
- `services/chat_service.py`: `_augment_capabilities_from_db()` skips adding DB services when `menu_runtime_enabled` is false.
- `handlers/booking_handler.py`: `_find_service()` and `_list_service_names()` depend on populated `service_catalog`.

2. Validator overriding transactional prompts
- `services/response_validator.py`: `_check_do_rules()` can trigger `policy_do_missing_detail_confirmation` and replace handler output with generic detail prompt.
- This breaks slot-filling continuity for booking when service slot is still missing.

3. Delivery intent misrouting
- `services/chat_service.py`: `_classify_intent()` has deterministic FAQ/policy/menu heuristics but no deterministic food-delivery override.
- LLM can classify delivery text with room mention into `room_service`.

4. Order matching limitations
- `handlers/order_handler.py`: `_handle_order_request()` resolves only menu item names (exact/fuzzy item name); no category intent resolution.
- `_find_fuzzy_item()` uses full-string similarity; weak on noisy multi-token requests.

5. Room number capture not surfaced in response context
- `services/conversation_memory_service.py`: room number is extracted into memory facts.
- `services/chat_service.py`: no deterministic profile-update branch to acknowledge and apply room number to `context.room_number` during idle turns.

## Fix Plan (Priority Order)

## P0 (Do first)

1. Service catalog availability hardening
- If `service_catalog` is empty, merge active DB restaurants for transactional intents even when `menu_runtime_enabled=false`.
- If still empty, return explicit "No configured services found" instead of "our available services".

2. Booking state prompt protection
- Skip `policy_do_missing_detail_confirmation` replacement when:
  - `context.pending_action` in booking slot-fill states, or
  - handler already returned a slot-specific next question.

3. Delivery/order intent override
- Add pre-classification rule:
  - if message contains order verbs (`order`, `deliver`, `send`) + food terms/menu token match => force `ORDER_FOOD`.

## P1

4. Category-aware ordering
- When item lookup fails, map tokens like `snacks`, `drink`, `beverage`, `dessert` to category query.
- Return top 5 available items and ask user to pick.

5. Better typo resilience
- Add token-level fuzzy scoring and n-best suggestions for unmatched items.
- Example fallback: "Did you mean: Chicken Alfredo Pasta, Pepperoni Pizza?"

6. Room number profile update flow
- On pattern `my room number is X`:
  - set `context.room_number = X`
  - persist context
  - respond with confirmation message.

## P2

7. RAG output post-filter
- Remove artifacts like `details incomplete`.
- For check-in/check-out questions, use deterministic extraction from matched chunks before LLM summarization.

8. Observability/UI clarity
- Surface `response_source`, `capability_denial`, and `routed_handler` in frontend debug card as "final action" (not only classifier intent).

## Regression Tests To Add

1. Booking flow with service present:
- `book table at kadak` -> `2am 5 guests` should validate hours, not ask service again.

2. Service list discovery:
- `what services are availabke` should return configured services (typo tolerant).

3. Delivery classification:
- `deliver chicken biryani from kadak to room 402` => `order_food` path.

4. Category order:
- `i want some snacks` returns options, not "item not found".

5. Room profile update:
- `my room number is 202` sets context and returns acknowledgment.

6. Validator non-overwrite:
- booking slot prompts are not replaced by generic DO-rule response.

## Expected Outcome After Fixes

1. Booking flows become stable and stateful.
2. Service prompts no longer collapse into "our available services".
3. Delivery/order intent routing improves significantly.
4. Typo and category requests convert to helpful suggestions.
5. Profile updates become first-class conversational memory actions.

