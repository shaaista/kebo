# Chat Response Mistakes Report

Date: 2026-02-17  
Project: `new_bot`  
Scope: Mistakes observed in the shared chat transcript (user message -> bot response)

## High-level pattern

Most failures come from four recurring causes:

1. Pending-action lock: once booking slot-filling starts, unrelated user queries are still routed into booking follow-up.
2. Missing workflow intent: "room stay booking" is not modeled, so those requests get forced into restaurant/table booking.
3. Over-aggressive response validation replacement: valid/partially-valid responses are overwritten with generic safe fallback.
4. Config/data inconsistency: menu/restaurant active status and policy text do not align across handlers and knowledge.

## Mistakes With Chat Response Text and Root Cause

| ID | User message | Bot response text | What is wrong | Why it is happening |
|---|---|---|---|---|
| M01 | `i want a room` | `Sure. Which restaurant would you like to book? We currently have: In-Room Dining, Kadak.` | User asked for a hotel room (stay), bot started restaurant table booking. | No "room stay booking" intent/handler in core intents; classifier bias pushes hospitality requests toward `table_booking` (`llm/client.py:201`, `config/business_config.json:209`). |
| M02 | `i mean a room` | `I couldn't match that restaurant. Available options: In-Room Dining, Kadak. Please tell me which one you prefer.` | Bot ignored correction and stayed in restaurant selection loop. | Pending slot-filling always takes priority if `pending_action` is booking detail (`services/chat_service.py:675`). |
| M03 | `a room to sleep` | `I couldn't match that restaurant. Available options: In-Room Dining, Kadak. Please tell me which one you prefer.` | Same loop; clear intent change not honored. | Same pending-action lock behavior (`services/chat_service.py:675`). |
| M04 | `i wanna book a room from feb 20-23` | `I couldn't match that restaurant. Available options: In-Room Dining, Kadak. Please tell me which one you prefer.` | Date range room-stay request treated as restaurant follow-up. | No room-stay flow + pending booking lock (`services/chat_service.py:675`, `llm/client.py:201`). |
| M05 | `i wanna go out ad eat what options do u have` | `I couldn't match that restaurant...` | Outside-dining/options question still trapped in same loop. | Pending-action lock prevents intent interruption (`services/chat_service.py:675`). |
| M06 | `restiarant outside hotel` | `I couldn't match that restaurant...` | Should provide outside options or say capability not supported, not slot-fill loop text. | Pending-action lock (`services/chat_service.py:675`). |
| M07 | `recommend a restaurant outside hotel` | `I couldn't match that restaurant...` | Recommendation query ignored; loop response repeated. | Pending-action lock (`services/chat_service.py:675`). |
| M08 | `what is the weather today` | `I couldn't match that restaurant...` | Completely unrelated FAQ got booking-loop response. | Pending-action lock (`services/chat_service.py:675`). |
| M09 | `check in check out timing` | `I couldn't match that restaurant...` | FAQ should answer timing, but loop persisted. | Pending-action lock (`services/chat_service.py:675`). |
| M10 | `spa availibiliyy?` | `I couldn't match that restaurant...` | Service-availability FAQ got wrong follow-up prompt. | Pending-action lock (`services/chat_service.py:675`). |
| M11 | `no chnage , reserve table for 2 at 2am` | `To help accurately, could you share your location or room number?` | Bot had enough booking details, but injected unrelated location/room question. | Response validator DO-rule replacement triggered with generic detail prompt (`services/response_validator.py:215`, `services/response_validator.py:229`). |
| M12 | `202` then `Yes, confirm` for `2am` Kadak booking | `Your table has been booked successfully!... Time: 2am` | Reservation confirmed for time outside Kadak hours. | Booking handler does not validate requested booking time against restaurant hours before confirmation (`handlers/booking_handler.py:238`). |
| M13 | `so how will i go at 2am when itll be closed` | `I'm not sure. Please contact a human for assistance regarding late-night access or services.` | Bot admits contradiction after making invalid booking. | Missing pre-confirmation hours validation and conflict handling in booking flow (`handlers/booking_handler.py:238`). |
| M14 | `show menu` (first two tries) | `I want to be accurate here. I can help with available in-hotel services or connect you with our team for special requests.` | Generic fallback replaced expected menu output. | Validator replaced response when inactive service looked promoted (`services/response_validator.py:180`, `services/response_validator.py:238`). |
| M15 | `recommend something nonveg` | `I'm not sure about specific non-vegetarian recommendations...` | Menu contains non-veg items, but bot failed to recommend. | No deterministic recommendation path over available menu/service data; fallback FAQ behavior. |
| M16 | `recommend someting spicy` | `Let me connect you with our team for better assistance.` | Escalation is unnecessary for simple menu recommendation. | No recommendation handler; classification/routing can fall to `human_request` path. |
| M17 | `i need poool pictures` | `I want to make sure I understood correctly...` | User asked for media; bot gave generic clarification without capability-aware response. | No media/image capability path; low-confidence handler sends generic clarify (`services/chat_service.py:605`). |
| M18 | `i wanna check in tom at 2pm` | `You can check in tomorrow at 2 PM... check in via WhatsApp...` | Partially correct timing, but WhatsApp instruction can be inconsistent with current config. | Knowledge text and runtime channel config are misaligned (`config/business_config.json:20`, KB source content). |
| M19 | `show all bnurger options` / `burger options` | Full menu shown without burger entries | User explicitly asked burger options; response ignored filter intent. | Menu handler only shows active restaurant menus; burger items may live in inactive outlet (`handlers/menu_handler.py:98`). |
| M20 | `burger` (order works) vs menu (burger missing) | `Here's your order summary... Classic Burger...` but menu lacks burger section | Contradictory behavior: item can be ordered but not discovered in menu view. | Order lookup does not enforce active restaurant availability while menu display does (`handlers/order_handler.py:113`, `handlers/menu_handler.py:98`). |
| M21 | `theres no burger only in this menu i want buger` | `I want to be accurate here...` | Complaint/request got generic safety fallback, not actionable next step. | Validator replacement path returns generic safe text when issues detected (`services/response_validator.py:232`). |
| M22 | `which location is this` -> `The location is Bangalore.` | Response may conflict with address/location context shown elsewhere (Mumbai fields). | Inconsistent business metadata between city and location/address creates answer drift. | Config inconsistency (`config/business_config.json:5`, `config/business_config.json:12`). |

## Additional quality issues seen in transcript

| ID | Observation | Why it matters | Why it is happening |
|---|---|---|---|
| Q01 | Intent labels/confidence shown as `unclear` even when slot-fill works (for `5`, `10pm`). | Analytics/debugging becomes misleading; response looked right but intent telemetry looked wrong. | UI displays raw classified intent/confidence, while routing may override by pending-action deterministic path (`static/js/chat.js:176`, `services/chat_service.py:675`). |
| Q02 | Character encoding glitch in cancellation text (`Iâ€™ve`). | Output quality issue in production UX. | String encoding artifact in booking handler text (`handlers/booking_handler.py:103`). |

## Code hotspots to prioritize

1. `services/chat_service.py:675` - allow intent interruption while in pending slot-filling.  
2. `handlers/booking_handler.py:238` - validate requested booking time/date against service hours before confirmation.  
3. `services/response_validator.py:215` - narrow DO-rule trigger by intent/state to avoid false replacements.  
4. `services/response_validator.py:180` - tune inactive-service promotion detection to reduce generic fallback overreach.  
5. `handlers/order_handler.py:113` + `handlers/menu_handler.py:98` - enforce consistent active-service/menu availability rules.

## Suggested acceptance checks after fixes

1. Room-stay queries never enter table-booking flow.
2. During booking slot-fill, unrelated FAQ queries can break out cleanly.
3. Booking for closed time windows is blocked with alternatives.
4. `show menu` never returns generic validator fallback when an active menu exists.
5. Burger discoverability and burger ordering behave consistently.

