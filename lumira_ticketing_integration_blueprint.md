# Lumira Ticketing Integration Blueprint (For `new_bot`)

Generated on: 2026-02-24  
Scope reviewed:
- `D:\Sana Hp Laptop Data\D Drive Data\kepslasw\lumira-develop` (primary for Engage)
- `D:\Sana Hp Laptop Data\D Drive Data\kepslasw\lumira-hotfix-9-Jan-GJ-bot` (Guest Journey parity)
- `D:\Sana Hp Laptop Data\D Drive Data\kepslasw\lumira-hotfix-9-Jan-GJ-bot\lumira-hotfix-9-Jan-GJ-bot` (deeper docs + duplicate code)
- Current bot: `d:\Sana Hp Laptop Data\D Drive Data\kepslasw\new_botv\new_bot`

## Latest Locked Dev Contract

- Use this file as the current source of truth for dev ticketing data and enum distributions:
  - `lumira_ticketing_dev_contract_locked.md`
- Practical execution/testing runbook:
  - `ticketing_integration_test_guide.md`
- Current safe runtime mode:
  - `TICKETING_LOCAL_MODE=true` stores ticket create/update in local JSON only (no external ticket DB/API write).

## 1) What Lumira Ticketing Actually Does

Lumira ticketing is not just a single API call. It is a flow with:
1. Intent + structured parse (`is_ticket_intent`, issue details, department, priority, etc.)
2. Context enrichment (guest, org/entity, room, outlet, phase, token/cost)
3. Optional route decision (`create` vs `update existing ticket` vs `acknowledge`)
4. External API call(s) (create/update ticket, optionally handoff to live agent)
5. Response back to user with `ticket_summary`

## 2) Ticketing APIs and Contracts Extracted from Lumira

### A) External APIs called by Lumira

1. **Create Ticket**
   - Method: `POST`
   - URL: `{TICKETING_BASE_URL}/insert/ticket.htm`
   - Used in:
     - `app/ticketing_system/services/ticket_creator.py` (develop + hotfix)

2. **Update Ticket**
   - Method: `PATCH`
   - URL: `{TICKETING_BASE_URL}/insert/ticket/{id}.htm`
   - Used in:
     - `app/ticketing_system/services/ticket_update.py` (develop + hotfix)

3. **Agent Handoff** (develop only)
   - Method: `POST`
   - URL: `{AGENT_HANDOFF_API_URL}`
   - Used in:
     - `app/ticketing_system/services/agent_handoff.py`
     - `app/engage/api/engage_bot_api.py`

### B) Incoming bot APIs where ticketing is triggered

1. **Guest Journey direct**
   - `POST /guest-journey/message` (plus backward-compat `/message`)

2. **Guest Journey configurator**
   - `POST /guest-journey/configurator-chat` (plus backward-compat `/new-message`)

3. **Engage**
   - `POST /engage-bot/message`

## 3) Payload Field Contract (Union from Branches)

### A) Create ticket payload fields used in Lumira

Core:
- `guest_id`
- `room_number`
- `organisation_id`
- `issue`
- `message`
- `sentiment_score`
- `department_allocated` (from `department_id`)
- `department_manager` (from `department_head`)
- `assigned_to`
- `priority`
- `categorization` (from `category`)
- `manager_notes`
- `created_at`
- `session_id`
- `phase`
- `ticket_status` (always `open`)
- `chatBot` (always `yes`)
- `outlet_id`

Branch-specific add-ons:
- `sub_categorization` (develop path)
- `source` (`whatsapp_bot` / `booking_bot`)
- `input_tokens`, `output_tokens`, `total_tokens`, `cost` (develop path)
- `sla_due_time` (hotfix path)
- `message_id`, `group_id` (develop Engage path)

### B) Update payload
- Path param: `id` in URL `/insert/ticket/{id}.htm`
- Body: `{ "manager_notes": "<note>" }`

### C) Handoff payload (develop)
- `from_responder`: `BOT`
- `to_responder`: `AGENT`
- `conversation_id`
- `session_id`
- `to_agent_id`
- `reason`

## 4) LLM/Tool Contracts that drive ticketing

### A) `create_ticket` function schema (Lumira)
- Defined in `app/ticketing_system/schemas/function_schema.py`
- Required fields include:
  - `room_number`, `guest_name`, `issue`, `sentiment_score`, `department_id`, `category`, `department_head`, `department_head_phone`, `priority`, `guest_id`, `phase`, `slaDue`, `outlet_id`
- Develop schema additionally requires `sub_category`.

### B) Structured parse model used upstream
- `ParsedIssue` includes:
  - `is_ticket_intent`, `response`, `issue`, `sentiment_score`, `department_id`, `department_head`, `department_head_phone`, `category`, `priority`, `slaDue`, `outlet_id`, `media`

### C) Update-vs-create router (develop GJ)
- `route_create_or_update_parsed(conversation, candidates)` decides:
  - `acknowledge` or `update` or `create`
- If `update`: returns `update_ticket_id` and `manager_notes` to patch existing open ticket.

## 5) DB Tables Required for Lumira-Style Ticketing

### Mandatory for smart routing + enrichment
1. `GHN_FEEDBACK_RESULTS.GHN_SURVEY_FMS_RI_ENTITY_MAPPING`
   - FMS entity to RI org mapping.
2. `GHN_PROD_BAK.DEPARTMENT_STAFF_CONFIG`
   - Department mapping and manager/agent assignment.
3. `GHN_PROD_BAK.DEPARTMENT_MASTER`
   - Department names/IDs.
4. `GHN_PROD_BAK.ORGANIZATION_OUTLETS`
   - Outlet IDs for outlet-aware ticketing.
5. `GHN_FF_PROD.GHN_FF_GUEST_INFO`
   - Guest profile (guest_id, room, entity).
6. `GHN_FF_PROD.GHN_FF_WHATSAPP_MESSAGES`
   - Conversation history used by parser/router context.
7. `GHN_PROD_BAK.GHN_OPERATION_TICKETS`
   - Candidate open tickets for update-vs-create routing.

### Engage/handoff specific
8. `GHN_PROD_BAK.DEPARTMENT_STAFF_CONFIG` (agent lookup for handoff)
9. `GHN_PROD_BAK.KEBO_ENGAGE_SESSIONS` (Engage conversation history context)

## 6) What Your `new_bot` Already Has

Already implemented (good foundation):
1. Lumira-style external API client:
   - Create ticket (`POST`)
   - Update ticket (`PATCH`)
   - Agent handoff (`POST`)
   - File: `services/ticketing_service.py`
2. Complaint flow for:
   - ticket create confirmation
   - ticket update notes
   - optional human handoff
   - File: `handlers/complaint_handler.py`
3. Human escalation flow:
   - auto-create ticket if missing + optional handoff
   - File: `handlers/escalation_handler.py`
4. Integration context ingestion from request metadata (`_integration`)
   - File: `services/chat_service.py`
5. Conversation memory stores `latest_ticket` / `ticket_history`
   - File: `services/conversation_memory_service.py`

## 7) Gaps to Close for Full Lumira-Parity

1. **No Lumira DB adapters yet** in `new_bot`
   - Missing reads for departments, outlets, candidate tickets, FMS-RI mapping.
2. **No create-vs-update ticket router** based on open ticket candidates.
3. **No Lumira-style structured parser contract** (`ParsedIssue` equivalent with department/outlet/sla fields).
4. **No Engage-specific ticket payload variant**
   - `message_id`, `group_id`, `source=booking_bot`, etc.
5. **No agent lookup query** (department/entity/group to `agent_id`) before handoff.
6. **No token/cost fields added into ticket payload** (Lumira develop sends these).
7. **Missing ticketing env vars in `.env.example`**
   - `TICKETING_BASE_URL`, `TICKETING_CREATE_PATH`, `TICKETING_UPDATE_PATH_TEMPLATE`, `TICKETING_TIMEOUT_SECONDS`, `AGENT_HANDOFF_API_URL`.
8. **No ticketing-focused tests yet**
   - No unit/integration tests for create/update/handoff success/failure/retry behavior.

## 8) Inconsistencies Found (Need Contract Freeze)

1. Update endpoint mismatch in docs vs code:
   - Code uses `PATCH /insert/ticket/{id}.htm`
   - Deep docs mention `/update/ticket.htm` (likely stale)
2. Create payload mismatch by branch:
   - develop includes `sub_categorization`, `source`, token/cost fields
   - hotfix includes `sla_due_time`, no `sub_categorization`
3. `GHN_CHATBOT_INFO` schema mismatch for prompt/hotel data elsewhere (already noted in prior DB report), which can indirectly affect ticket parser quality.

## 9) Missing Information (Searched Deep, Still Not Found)

These are not clearly specified in code/docs and must be confirmed with the ticketing backend owner:
1. Does ticketing API require auth headers/signature/IP allowlist?
2. Exact enum values accepted for `phase`, `priority`, `categorization`, `ticket_status`.
3. Official response schema for create/update/handoff (all possible ID keys/status keys).
4. Whether `sla_due_time` is mandatory in production.
5. Whether `sub_categorization` is accepted/required in production.
6. Whether `assigned_to` input should be provided by bot or always server-assigned.
7. Idempotency expectations for repeated create requests (same issue/session).

## 10) Smart Integration Plan for `new_bot`

### Phase 1: Contract and adapter layer (must do first)
1. Freeze external API contract with backend team (section 9).
2. Create `integrations/lumira_ticketing_repository.py` with read-only methods:
   - `fetch_departments_of_entity(entity_id)`
   - `fetch_outlets_of_entity(entity_id)`
   - `fetch_candidate_tickets(guest_id, room_number)`
   - `fetch_agent_for_handover(department_id, entity_id=None, group_id=None)`
   - `fetch_ri_entity_id_from_mapping(fms_entity_id)` (if needed)
3. Add ticketing env vars to `.env.example` and config docs.

### Phase 2: Decision engine + payload normalizer
1. Add `services/ticketing_router_service.py`:
   - decision: `acknowledge|update|create`
2. Add payload normalizer in `ticketing_service`:
   - support both contracts (`sla_due_time` + `sub_categorization`)
   - optional telemetry fields (`input_tokens`, `output_tokens`, `total_tokens`, `cost`)
3. Add branch-safe response normalization for `ticket_id` and `assigned_id`.

### Phase 3: Intent/parser enrichment
1. Extend complaint intent entities to include:
   - `department_id`, `department_head`, `department_head_phone`, `outlet_id`, `phase`, `sla_due_time`, `sub_category`.
2. Inject department/outlet context into parser prompt and extraction path.
3. Use router before create call:
   - open candidate + similar message => acknowledge/update instead of duplicate create.

### Phase 4: Engage parity
1. Add Engage-specific ticket payload builder:
   - `group_id`, `message_id`, `source=booking_bot`, pre-booking phase.
2. Add optional handoff path with DB-based `agent_id` resolution.
3. Keep this behind feature flags until validated.

### Phase 5: Reliability and rollout
1. Add tests:
   - unit: payload builder, response parser, router decisions
   - integration: create/update/handoff with mocked HTTP
   - failure paths: timeout/5xx/malformed JSON
2. Add observability:
   - log `ticket_operation`, `ticket_id`, `status_code`, latency, retry count
3. Roll out in sequence:
   - Stage A: create only
   - Stage B: update router enabled
   - Stage C: handoff enabled

## 11) Recommended Execution Order (Practical)

1. Implement Phase 1 + Phase 2 first (low risk, high value).
2. Then Phase 3 for smart dedupe/update behavior.
3. Then Engage parity (Phase 4) if needed immediately.
4. Phase 5 continuously during rollout.

---

## Bottom Line

Your `new_bot` already has the core API client and complaint/handoff flow.  
To match Lumira-level ticketing quality, you now mainly need:
- Lumira DB enrichment adapters,
- create-vs-update decision routing,
- parser/entity enrichment for department/outlet/SLA,
- contract freeze for payload/response ambiguities.
