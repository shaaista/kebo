# Engage + Guest Journey DB Tables Report

Generated on: 2026-02-24

## Scope

- Engage: `lumira-develop` is the primary reference.
- Guest Journey: combined reference from `lumira-develop` + `lumira-hotfix-9-Jan-GJ-bot`.
- Focus is only DB tables used by Engage and Guest Journey code paths.

## Quick Clarification

- This report includes existing tables you can directly reuse.
- It also includes conditional existing tables (needed only if you keep optional flows).
- It now includes additional tables recommended to create for the new bot.

## 1) Engage (Develop Primary)

| Table | Required | Where used | Used for | Read/Write |
|---|---|---|---|---|
| `GHN_PROD_BAK.KEBO_ENGAGE_SESSIONS` | Yes | `app/extensions/db/engage_sql.py` (`fetch_last_n_messages`), `app/engage/api/engage_bot_api.py` | Pull conversation history (`session_id + conversation_id`) for context/routing. | Read (write helper exists but not used in develop API path) |
| `GHN_PROD_BAK.GHN_CHAT_BOT_CUSTOMER_DETAILS` | Yes | `app/extensions/db/engage_sql.py` (`fetch_engage_user_details`), `app/engage/api/engage_bot_api.py` | Fetch customer identity (name/email/phone) to decide form/handoff/ticket behavior. | Read |
| `GHN_PROD_BAK.GHN_CHATBOT_INFO` | Yes | `app/extensions/db/engage_sql.py` (`fetch_editable_config_and_data`, `fetch_group_prompt_template`), `app/engage/llm_engine/prompt_engine.py` | Source of prompt/hotel configuration JSON for entity/group prompting. | Read |
| `GHN_PROD_BAK.GHN_CHATBOT_FORMS` | Yes (if form rendering is kept) | `app/extensions/db/engage_sql.py` (`fetch_forms_for_widget`), `app/engage/api/engage_bot_api.py` | Widget form catalog (`Title`, `Form_Key`, `Type`) used by form-routing logic. | Read |
| `GHN_FEEDBACK_RESULTS.GHN_SURVEY_FMS_RI_ENTITY_MAPPING` | Yes | `app/extensions/db/engage_sql.py` (`fetch_fms_entity_mapping`) | Map group-selected org (`ri_entity_id`) to `fms_entity_id` for entity-level continuation. | Read |
| `GHN_PROD_BAK.GHN_ORGANIZATION` | Yes (for group flow) | `app/extensions/db/engage_sql.py` (`fetch_group_data`) | Group property list and org metadata for group-level recommendation/routing. | Read |
| `GHN_PROD_BAK.GHN_ORGANIZATION_BRAND` | Yes (for group flow) | `app/extensions/db/engage_sql.py` (`fetch_group_data`) | Brand-to-group linkage used during group property resolution. | Read |
| `GHN_PROD_BAK.GHN_ORGANIZATION_GROUP` | Yes (for group flow) | `app/extensions/db/engage_sql.py` (`fetch_group_data`) | Group filtering (`group_id`) and group-name context. | Read |
| `GHN_PROD_BAK.GHN_GEO_AREA_MASTER` | Yes (for group flow) | `app/extensions/db/engage_sql.py` (`fetch_group_data`) | Area-to-city join for city extraction. | Read |
| `GHN_PROD_BAK.GHN_GEO_CITY_MASTER` | Yes (for group flow) | `app/extensions/db/engage_sql.py` (`fetch_group_data`) | City context for property filtering and response metadata. | Read |
| `GHN_PROD_BAK.GHN_GEO_COUNTRY_MASTER` | Yes (for group flow) | `app/extensions/db/engage_sql.py` (`fetch_group_data`) | Country context for property filtering and response metadata. | Read |
| `GHN_PROD_BAK.DEPARTMENT_STAFF_CONFIG` | Yes (for ticket/handoff enabled flow) | `app/extensions/db/engage_sql.py` (`fetch_departments_for_group`, `fetch_agent_for_handover`) | Department discovery and agent assignment for handoff/ticket path. | Read |
| `GHN_PROD_BAK.DEPARTMENT_MASTER` | Yes (for ticket/handoff enabled flow) | `app/extensions/db/engage_sql.py` (`fetch_departments_for_group`) | Department naming/ID mapping for parser and routing. | Read |
| `GHN_PROD_BAK.ORGANIZATION_OUTLETS` | Yes (if outlet-aware parsing is kept) | `app/extensions/db/guest_journey_sql.py` (`fetch_outlets_of_entity`) imported by Engage API | Provides outlet IDs/names used in parsing and ticket payload enrichment. | Read |

### Engage Notes

- `GHN_PROD_BAK.GHN_CHATBOT_MEDIA` exists in Engage SQL helper but is not actively used by the develop Engage API path.
- If you keep only entity-level flow and remove group-level routing, some group/geo tables become non-required.

## 2) Guest Journey (Develop + Hotfix Combined)

| Table | Required | Where used | Used for | Read/Write |
|---|---|---|---|---|
| `GHN_FF_PROD.GHN_FF_GUEST_INFO` | Yes | `app/extensions/db/guest_journey_sql.py` (`fetch_guest_info`, `upsert_guest_preferences` in develop), `app/guest_journey/api/guest_journey_bot_api.py` | Guest profile lookup (name/room/dates/entity); in develop also persists merged guest preferences (`GUEST_PREFERENCES`). | Read + Write (develop) |
| `GHN_FF_PROD.GHN_FF_WHATSAPP_MESSAGES` | Yes | `app/extensions/db/guest_journey_sql.py` (`fetch_last_n_messages`, `fetch_tagged_msg_timestamp`, `fetch_messages_after`, `fetch_distinct_subcategories` in develop), `app/guest_journey/api/guest_journey_bot_api.py` | WhatsApp conversation history for context, interactive reconfirm flow, and subcategory mining (develop). | Read |
| `GHN_PROD_BAK.GHN_CHATBOT_INFO` | Yes | `app/extensions/db/guest_journey_sql.py` (`fetch_editable_config_and_data`), `app/guest_journey/llm_engine/hotel_prompt_engine.py` | Pulls hotel/prompt config used to build guest-journey system prompt. | Read |
| `GHN_PROD_BAK.GHN_CHATBOT_MEDIA` | Yes | `app/extensions/db/guest_journey_sql.py` (`fetch_links_of_entity`), `app/guest_journey/api/guest_journey_bot_api.py` | Menu/media URL retrieval for menu-request responses. | Read |
| `GHN_FEEDBACK_RESULTS.GHN_SURVEY_FMS_RI_ENTITY_MAPPING` | Yes | `app/extensions/db/guest_journey_sql.py` (`fetch_ri_entity_id_from_mapping`, `fetch_departments_of_entity`) | FMS-to-org mapping and department retrieval joins. | Read |
| `GHN_PROD_BAK.DEPARTMENT_STAFF_CONFIG` | Yes | `app/extensions/db/guest_journey_sql.py` (`fetch_departments_of_entity`) | Department candidates used by parser/tool calls. | Read |
| `GHN_PROD_BAK.DEPARTMENT_MASTER` | Yes | `app/extensions/db/guest_journey_sql.py` (`fetch_departments_of_entity`) | Department names/IDs for issue routing. | Read |
| `GHN_PROD_BAK.ORGANIZATION_OUTLETS` | Yes | `app/extensions/db/guest_journey_sql.py` (`fetch_outlets_of_entity`), `app/guest_journey/api/guest_journey_bot_api.py` | Outlet mapping used by parser and ticket payload enrichment. | Read |
| `GHN_PROD_BAK.WHATSAPP_CHATBOT_MESSAGES` | Conditional (required if configurator chat endpoints are retained) | `app/extensions/db/guest_journey_sql.py` (`fetch_last_few_messages`, `insert_message_into_db`), `app/guest_journey/api/guest_journey_bot_api.py` (`/configurator-chat`, `/new-message`) | Stores non-WhatsApp configurator chat transcript/history. | Read + Write |
| `GHN_PROD_BAK.GHN_OPERATION_TICKETS` | Conditional (develop behavior) | `app/extensions/db/guest_journey_sql.py` (`fetch_candidate_tickets`), `app/guest_journey/api/guest_journey_bot_api.py` (develop uses create-vs-update router) | Fetches open/in-progress tickets for update-vs-create decision. | Read |
| `LUMIRA_llm_sessions` | Conditional (recommended ops table) | `app/guest_journey/utils/push_sessions_to_db.py`, `tasks/llm_session_tasks.py` | Persists expired Redis session summary (session tokens/cost/guest/entity metadata). | Write |
| `LUMIRA_llm_session_models` | Conditional (recommended ops table) | `app/guest_journey/utils/push_sessions_to_db.py`, `tasks/llm_session_tasks.py` | Persists per-model token/cost breakdown for each expired session. | Write |

### Guest Journey Notes

- Hotfix root API has ticket update routing commented; develop enables `fetch_candidate_tickets` usage.
- If you disable configurator chat endpoints, `GHN_PROD_BAK.WHATSAPP_CHATBOT_MESSAGES` is not needed.
- If you disable session cost analytics tasks, `LUMIRA_llm_sessions` and `LUMIRA_llm_session_models` are not needed.

## 3) Cross-Branch Compatibility Risks You Should Resolve Early

### A) `GHN_CHATBOT_INFO` schema contract differs by branch

- Hotfix guest journey expects URL fields:
  - `hoteldata_url`, `prompt_url` (and `source='Lumira'` filter)
- Develop engage/guest journey expects JSON fields:
  - `hotel_data_json`, `prompt_data_json`

Recommendation:
- Standardize your new bot on one contract (prefer JSON contract if you are building enhanced flow), or keep a compatibility layer that can read either schema.

### B) Engage message persistence behavior differs

- Hotfix Engage writes directly into `KEBO_ENGAGE_SESSIONS`.
- Develop Engage API primarily reads history; write helper exists but is not used in the main path.

Recommendation:
- Decide ownership of engage message writes (upstream Java/event pipeline vs this service) and keep one source of truth.

## 4) Existing Tables To Reuse (Final Split)

### 4.1 Must-Have Existing Tables (for your stated Engage + Guest Journey runtime)

1. `GHN_PROD_BAK.KEBO_ENGAGE_SESSIONS`
2. `GHN_PROD_BAK.GHN_CHAT_BOT_CUSTOMER_DETAILS`
3. `GHN_PROD_BAK.GHN_CHATBOT_INFO`
4. `GHN_PROD_BAK.GHN_CHATBOT_FORMS`
5. `GHN_FEEDBACK_RESULTS.GHN_SURVEY_FMS_RI_ENTITY_MAPPING`
6. `GHN_FF_PROD.GHN_FF_GUEST_INFO`
7. `GHN_FF_PROD.GHN_FF_WHATSAPP_MESSAGES`
8. `GHN_PROD_BAK.GHN_CHATBOT_MEDIA`
9. `GHN_PROD_BAK.DEPARTMENT_STAFF_CONFIG`
10. `GHN_PROD_BAK.DEPARTMENT_MASTER`
11. `GHN_PROD_BAK.ORGANIZATION_OUTLETS`

### 4.2 Conditional Existing Tables (only if you keep these flows)

1. `GHN_PROD_BAK.GHN_ORGANIZATION` (Engage group-level recommendation/routing)
2. `GHN_PROD_BAK.GHN_ORGANIZATION_BRAND` (Engage group/brand mapping)
3. `GHN_PROD_BAK.GHN_ORGANIZATION_GROUP` (Engage group filtering)
4. `GHN_PROD_BAK.GHN_GEO_AREA_MASTER` (Engage city derivation for group flow)
5. `GHN_PROD_BAK.GHN_GEO_CITY_MASTER` (Engage city filtering/metadata)
6. `GHN_PROD_BAK.GHN_GEO_COUNTRY_MASTER` (Engage country filtering/metadata)
7. `GHN_PROD_BAK.WHATSAPP_CHATBOT_MESSAGES` (if configurator chat endpoints are retained)
8. `GHN_PROD_BAK.GHN_OPERATION_TICKETS` (if develop update-vs-create routing is retained)
9. `LUMIRA_llm_sessions` (if session analytics archival is retained)
10. `LUMIRA_llm_session_models` (if model-level token/cost archival is retained)

## 5) Extra Tables To Create For The New Bot

These are not from the old bot, but are strongly recommended for a clean enhanced architecture.

| Proposed table | Why create it | Minimum fields |
|---|---|---|
| `NB_BOT_SESSIONS` | Unified session record across Engage/GJ/channels instead of relying only on Redis + fragmented legacy logs. | `id`, `session_id`, `channel`, `user_key`, `entity_id`, `group_id`, `started_at`, `last_activity_at`, `status`, `metadata_json` |
| `NB_BOT_MESSAGES` | Canonical message ledger for all user/bot turns; supports replay, debugging, and analytics. | `id`, `session_id`, `direction` (`user`/`bot`), `message_text`, `message_type`, `external_message_id`, `created_at`, `tokens_in`, `tokens_out`, `cost`, `raw_payload_json` |
| `NB_BOT_PENDING_STATE` | Durable pending-action/slot state (so flow is deterministic even across restarts). | `id`, `session_id`, `flow_name`, `pending_slot`, `state_json`, `expires_at`, `updated_at` |
| `NB_BOT_MEMORY_FACTS` | Store extracted user/guest facts (preferences, confirmed details) for continuity and personalization. | `id`, `session_id`, `guest_id`, `fact_type`, `fact_key`, `fact_value_json`, `confidence`, `source_message_id`, `updated_at` |
| `NB_BOT_TICKET_LINKS` | Explicit mapping from bot sessions/messages to operation tickets and handoff actions. | `id`, `session_id`, `message_id`, `ticket_id`, `department_id`, `agent_id`, `action_type`, `status`, `created_at`, `updated_at` |
| `NB_BOT_AUDIT_EVENTS` | System/event audit trail (routing decisions, tool calls, failures, guardrail hits). | `id`, `session_id`, `event_type`, `event_source`, `payload_json`, `severity`, `created_at` |

### New Table Priority

1. Must create first: `NB_BOT_SESSIONS`, `NB_BOT_MESSAGES`, `NB_BOT_PENDING_STATE`
2. Next: `NB_BOT_MEMORY_FACTS`, `NB_BOT_TICKET_LINKS`
3. Operational: `NB_BOT_AUDIT_EVENTS`
