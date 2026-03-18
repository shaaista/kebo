# Ticketing System Full Reference (Current Implementation)

Last updated: 2026-03-18

This document describes how ticketing currently works in this repository, including:
- inbound APIs,
- outbound ticketing/handoff URLs,
- payloads and normalization,
- routing and enrichment logic,
- local-mode storage and logs.

It is based on the current code paths in:
- `api/routes/chat.py`
- `api/routes/lumira_compat.py`
- `api/routes/admin.py`
- `services/ticketing_service.py`
- `handlers/complaint_handler.py`
- `handlers/booking_handler.py`
- `handlers/order_handler.py`
- `handlers/room_service_handler.py`
- `handlers/escalation_handler.py`
- `integrations/lumira_ticketing_repository.py`

---

## 1) High-Level Architecture

1. Client sends message to one of the bot APIs.
2. Message is processed by chat orchestration and routed to handlers.
3. Ticketing agent logic determines if ticketing should activate.
4. Handler builds ticket payload via `ticketing_service.build_lumira_ticket_payload(...)`.
5. Ticket operation executes:
   - create (new ticket),
   - update (append notes),
   - acknowledge (no new ticket),
   - optional human handoff.
6. Response metadata carries ticket status and IDs.
7. Debug and diagnostics logs record complete trace.

---

## 2) Inbound APIs (Your Bot Endpoints)

### 2.1 Core Chat API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/chat/message` | Main chat endpoint; ticketing flows are executed from this pipeline when applicable. |

Notes:
- This path is under gateway auth/rate-limit middleware (`/api/chat` protected prefix).
- Request/response gets `turn_trace_id` in metadata for diagnostics.

### 2.2 Lumira Compatibility APIs

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/guest-journey/message` | Lumira guest journey contract -> internally mapped to `ChatRequest`. |
| `POST` | `/engage-bot/message` | Lumira engage contract -> internally mapped to `ChatRequest`. |

Important behavior:
- `/engage-bot/message` rejects request with `400` unless body includes at least one of:
  - `entity_id` / `entityId`
  - `group_id` / `groupId`
- Adapter maps `entity_id` into metadata and also sets `organisation_id` by default.

### 2.3 Admin Local Tickets API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/admin/api/tickets` | Lists locally stored tickets from JSON file. |

Current implementation detail:
- This endpoint reads fixed path `./data/ticketing/local_tickets.json` (not the env-configured local store path).

---

## 3) Outbound Ticketing/Handoff APIs (External URLs)

All outbound calls are made by `services/ticketing_service.py`.

### 3.1 Create Ticket

- Method: `POST`
- URL composition:
  - `base = TICKETING_BASE_URL` (trailing slash removed)
  - `path = TICKETING_CREATE_PATH` (default `/insert/ticket.htm`)
  - final URL: `{base}{path}`

### 3.2 Update Ticket

- Method: `PATCH`
- URL composition:
  - `base = TICKETING_BASE_URL`
  - `template = TICKETING_UPDATE_PATH_TEMPLATE` (default `/insert/ticket/{ticket_id}.htm`)
  - `template.format(ticket_id=tid, id=tid)` is attempted
  - if formatting fails, fallback path: `/insert/ticket/{tid}.htm`
  - final URL: `{base}{resolved_path}`

### 3.3 Human Handoff

- Method: `POST`
- URL:
  - `AGENT_HANDOFF_API_URL` (used as-is)

Handoff payload:
```json
{
  "from_responder": "BOT",
  "to_responder": "AGENT",
  "conversation_id": "...",
  "session_id": "...",
  "to_agent_id": "...",
  "reason": "..."
}
```

---

## 4) Local Mode vs API Mode

### 4.1 Mode Switch

- `TICKETING_LOCAL_MODE=true`
  - No outbound HTTP call.
  - Create/update persisted to local JSON + CSV.
- `TICKETING_LOCAL_MODE=false`
  - Uses outbound HTTP URLs in Section 3.

### 4.2 Local Files

- JSON store path:
  - `TICKETING_LOCAL_STORE_FILE`
  - default fallback in service: `./data/ticketing/local_tickets.json`
- CSV path:
  - `TICKETING_LOCAL_CSV_FILE`
  - if empty, derived from JSON path (`.json` -> `.csv`)

### 4.3 Local ID and Sync

- Ticket IDs are generated as `LOCAL-<n>`.
- JSON store is source of truth.
- CSV is synced from JSON after each create/update.
- If primary CSV path is locked, mirror file `<name>_mirror.csv` is written.

---

## 5) Ticket Payload Contract (Current Builder)

Ticket payload is produced by `build_lumira_ticket_payload(...)`.

Core fields sent:

| Field | Source/Rule |
|---|---|
| `guest_id` | `_integration.guest_id` -> `_integration.user_id` -> `_integration.wa_number` -> `context.guest_phone` |
| `room_number` | `context.room_number` -> `_integration.room_number` |
| `organisation_id` | `_integration.organisation_id` -> `_integration.organization_id` -> `_integration.org_id` -> `_integration.entity_id` -> `context.hotel_code` |
| `issue` | explicit `issue` (fallback to `message`) |
| `message` | explicit `message` (fallback to `issue`) |
| `priority` | normalized to `CRITICAL/HIGH/MEDIUM/LOW` |
| `categorization` | normalized (prefers `request/complaint/upsell/inquiry`; unknown passes through) |
| `sub_categorization` | explicit sub-category |
| `phase` | normalized to `Booking/Pre Checkin/During Stay/Post Checkout/Pre Booking` |
| `ticket_status` | normalized; default `open` |
| `source` | resolved from explicit source, integration, flow/channel |
| `session_id` | current conversation session |
| `created_at` | UTC timestamp string `HH:MM:SS DD-MM-YYYY` |

Backward-compatible alias fields also added:
- `department_id` = `department_allocated`
- `category` = `categorization`
- `sub_category` = `sub_categorization`

Optional fields added if present:
- `group_id`
- `message_id`
- `input_tokens`
- `output_tokens`
- `total_tokens`
- `cost`

---

## 6) Source, Phase, and Status Normalization

### 6.1 Source Resolution

Resolution priority:
1. explicit `source` argument,
2. `_integration.ticket_source` or `_integration.source`,
3. flow-based default:
   - engage/booking flow -> `booking_bot`
4. channel default:
   - whatsapp/wa -> `whatsapp_bot`
   - otherwise -> `manual`

### 6.2 Phase Normalization

Normal output values:
- `Booking`
- `Pre Checkin`
- `During Stay`
- `Post Checkout`
- `Pre Booking`

### 6.3 Status Normalization

Input values map to:
- `open`
- `in_progress`
- `closed`
- `cancel`
- `breached`

Unknown values default to `open`.

---

## 7) Ticket Activation and Routing Logic

### 7.1 Ticketing Enablement Gate

`ticketing_service.is_ticketing_enabled(...)` returns true only when:
1. `TICKETING_PLUGIN_ENABLED=true`, and
2. either:
   - local mode is enabled, or
   - `TICKETING_BASE_URL` is configured.

Then tool-level config can further disable ticketing if `ticketing`/`ticket_create` tool exists with `enabled=false`.

### 7.2 Configured Case Match Gate

Handlers commonly require a configured ticketing-case match before create.
Matching path:
1. LLM match (`ticketing_case_match_use_llm`), then
2. optional deterministic fallback (`ticketing_case_match_fallback_enabled`).

### 7.3 Smart Router for Existing Tickets (Complaint Flow)

Complaint flow may route to:
- `acknowledge` (same issue, no new detail),
- `update` (same issue, new details),
- `create` (new issue).

Router service:
- LLM-first decision (`ticketing_smart_routing_use_llm`),
- deterministic fallback by text similarity thresholds:
  - `TICKETING_ROUTER_ACK_SIMILARITY` (default `0.88`)
  - `TICKETING_ROUTER_UPDATE_SIMILARITY` (default `0.55`)

### 7.4 Service-Level Ticketing Disabled Gate

Before create, `detect_service_ticketing_disabled(payload)` checks if matched service has `ticketing_enabled=false` in admin config.
- If disabled, create is blocked with:
  - `success=false`
  - `status_code=409`
  - `error="phase_service_ticketing_disabled"`

---

## 8) Handlers That Can Create or Update Tickets

| Handler | Action | Typical Category/Sub-category |
|---|---|---|
| `ComplaintHandler` | create/update/acknowledge/escalation flow | complaint/request with resolved sub-category |
| `BookingHandler` | create ticket on confirmed booking (non-blocking) | `request` + inferred booking sub-category |
| `OrderHandler` | create ticket on confirmed food order (non-blocking) | `request` + `order_food` |
| `RoomServiceHandler` | create ticket for actionable in-room request | `request` + mapped room-service sub-category |
| `EscalationHandler` | may create ticket for human request + optional handoff | `request` + `human_handoff` |

Non-blocking behavior:
- Booking/order/room-service ticket failures do not block guest confirmation response.

---

## 9) DB Enrichment and Lookup Calls (No HTTP URL; SQL-backed)

Repository: `integrations/lumira_ticketing_repository.py`

| Method | Purpose | Main Tables |
|---|---|---|
| `fetch_departments_of_entity` | department + manager agent mapping | `GHN_SURVEY_FMS_RI_ENTITY_MAPPING`, `DEPARTMENT_STAFF_CONFIG`, `DEPARTMENT_MASTER` |
| `fetch_outlets_of_entity` | outlet mapping | `ORGANIZATION_OUTLETS` |
| `fetch_candidate_tickets` | open/in-progress ticket candidates | `GHN_OPERATION_TICKETS` |
| `fetch_agent_for_handover` | resolve handoff agent id | `DEPARTMENT_STAFF_CONFIG` |
| `fetch_ri_entity_id_from_mapping` | FMS -> RI entity mapping | `GHN_SURVEY_FMS_RI_ENTITY_MAPPING` |
| `fetch_guest_profile` | hydrate room/guest details | `GHN_FF_GUEST_INFO` |

---

## 10) Response Parsing and Error Handling

### 10.1 Create/Update/Handoff HTTP response parsing

`_request_json(...)`:
- success if `200 <= status_code < 300`
- ticket id extracted from first available key:
  - `ticket_id`, `ticketId`, `id`, `ticketNo`, `ticket_number`
- assigned id extracted from:
  - `assignedId`, `assigned_id`, `agent_id`, `to_agent_id`

### 10.2 Failure behavior

- network/HTTP exception -> `success=false`, error string captured.
- non-2xx -> `success=false`, error from payload keys `error/message/detail` or raw text.
- missing `TICKETING_BASE_URL` in API mode -> immediate failure.
- missing `AGENT_HANDOFF_API_URL` for handoff -> immediate failure.

---

## 11) Current Ticketing-Related Environment Variables

### 11.1 Core endpoint variables

```env
TICKETING_BASE_URL=
TICKETING_CREATE_PATH=/insert/ticket.htm
TICKETING_UPDATE_PATH_TEMPLATE=/insert/ticket/{ticket_id}.htm
AGENT_HANDOFF_API_URL=
TICKETING_TIMEOUT_SECONDS=10
```

### 11.2 Mode and storage

```env
TICKETING_LOCAL_MODE=false
TICKETING_LOCAL_STORE_FILE=/tmp/local_tickets.json
TICKETING_LOCAL_CSV_FILE=/tmp/local_tickets.csv
```

### 11.3 Enablement and routing

```env
TICKETING_PLUGIN_ENABLED=true
TICKETING_PLUGIN_TAKEOVER_MODE=false
TICKETING_SMART_ROUTING_ENABLED=true
TICKETING_SMART_ROUTING_USE_LLM=true
TICKETING_ROUTER_MODEL=gpt-4o-mini
TICKETING_ROUTER_ACK_SIMILARITY=0.88
TICKETING_ROUTER_UPDATE_SIMILARITY=0.55
```

### 11.4 Enrichment and policy controls

```env
TICKETING_ENRICHMENT_ENABLED=true
TICKETING_AUTO_CREATE_ON_ACTIONABLE=true
TICKETING_UPDATE_WINDOW_MINUTES=2
TICKETING_UPDATE_WINDOW_LLM_ASSESSMENT_ENABLED=true
TICKETING_IDENTITY_GATE_ENABLED=false
TICKETING_IDENTITY_GATE_PREBOOKING_ONLY=true
TICKETING_IDENTITY_REQUIRE_NAME=true
TICKETING_IDENTITY_REQUIRE_PHONE=true
```

### 11.5 LLM helper controls

```env
TICKETING_CASE_MATCH_USE_LLM=true
TICKETING_CASE_MATCH_MODEL=gpt-4o-mini
TICKETING_CASE_MATCH_FALLBACK_ENABLED=true
TICKETING_SUBCATEGORY_LLM_ENABLED=false
TICKETING_SUBCATEGORY_MODEL=gpt-4o-mini
TICKETING_GUEST_PREFERENCES_ENABLED=false
TICKETING_GUEST_PREFERENCES_USE_LLM=false
TICKETING_GUEST_PREFERENCES_MODEL=gpt-4o-mini
```

### 11.6 Debug logging

```env
TICKETING_DEBUG_LOG_ENABLED=true
TICKETING_DEBUG_LOG_FILE=./logs/ticketing_debug.jsonl
```

### 11.7 Chat test phase profile mapping (QA helper)

Used only by `/chat` test UI to auto-inject `guest_id`/`entity_id` by selected phase.

```env
CHAT_TEST_PHASE_PROFILE_AUTO_APPLY=true
CHAT_TEST_PHASE_PROFILES_JSON={"pre_checkin":{"guest_id":"921346","entity_id":"5703","organisation_id":"5703","ticket_source":"whatsapp_bot"},"during_stay":{"guest_id":"921348","entity_id":"5703","organisation_id":"5703","ticket_source":"whatsapp_bot"},"post_checkout":{"guest_id":"921347","entity_id":"5703","organisation_id":"5703","ticket_source":"whatsapp_bot"}}
```

API endpoint used by test UI:
- `GET /api/chat/test-profiles`

---

## 12) Operational Logs and Diagnostics

### 12.1 Ticketing debug stream

File:
- `./logs/ticketing_debug.jsonl`

Common events:
- `ticket_create_requested`
- `ticket_create_api_completed`
- `ticket_create_local_completed`
- `ticket_update_requested`
- `ticket_update_api_completed`
- `ticket_update_local_completed`
- `ticketing_http_request`
- `ticketing_http_response`
- `ticketing_http_exception`
- `handoff_requested`
- `handoff_completed`

### 12.2 Turn-level diagnostics

File:
- `./logs/turn_diagnostics.jsonl`

Useful for correlating:
- user input,
- phase selection,
- LLM calls,
- orchestration decisions,
- final response metadata.

---

## 13) End-to-End URL Examples

If:
- `TICKETING_BASE_URL=https://dev-reviews.kepsla.com:18080/kepsla-rep-dashboard-svc`
- `TICKETING_CREATE_PATH=/insert/ticket.htm`
- `TICKETING_UPDATE_PATH_TEMPLATE=/insert/ticket/{ticket_id}.htm`

Then:
- create URL:
  - `POST https://dev-reviews.kepsla.com:18080/kepsla-rep-dashboard-svc/insert/ticket.htm`
- update URL for ticket `5477`:
  - `PATCH https://dev-reviews.kepsla.com:18080/kepsla-rep-dashboard-svc/insert/ticket/5477.htm`

Handoff URL is whatever is configured in:
- `AGENT_HANDOFF_API_URL`

---

## 14) Important Current Caveats

1. `organisation_id` is the outbound payload key for entity identity.  
   Source chain can fall back to `context.hotel_code` if no entity/org is available.
2. `/engage-bot/message` requires `entity_id` or `group_id` in request body.
3. `/admin/api/tickets` currently reads hardcoded local JSON path (`./data/ticketing/local_tickets.json`), not env path.
4. Handoff runs only when `AGENT_HANDOFF_API_URL` is set and handoff capability/tool gates pass.
