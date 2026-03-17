# KePSLA Conversational AI - New Bot Development Progress

**Created**: February 3, 2026
**Purpose**: Build a new bot that fixes all Lumira problems and is fully PRD-compliant
**Status**: Active Implementation Phase (~83% Core Complete)

## Latest Update (March 13, 2026)

### Bug Fixes: KB Enrichment Startup Error + Context Length Exceeded

#### Fix 1 — `ConfigService.get_full_kb_text()` missing `max_chars` parameter
**File**: `services/config_service.py` (line 3234)

**Problem**: `get_full_kb_text()` had no `max_chars` parameter, but all callers in `main.py` and `llm_orchestration_service.py` were passing `max_chars=...`. This caused a `TypeError` on every call, silently caught and logged as:
```
⚠️  KB enrichment failed (non-fatal): ConfigService.get_full_kb_teext() got an unexpected keyword argument 'max_chars'
```
KB enrichment was completely non-functional as a result.

**Fix**: Added `max_chars: int | None = None` to the signature, with truncation applied when provided.

```python
# Before
def get_full_kb_text(self) -> str:

# After
def get_full_kb_text(self, max_chars: int | None = None) -> str:
    ...
    if max_chars is not None:
        result = result[:max_chars]
    return result
```

**Callers now working correctly**:
- `main.py:37` — `max_chars=1000`
- `llm_orchestration_service.py:414` — `max_chars=40_000`
- `llm_orchestration_service.py:1148` — `max_chars=40_000`
- `llm_orchestration_service.py:1544` — `max_chars=60_000`
- `admin.py:1266` — no max_chars (full text, unchanged)

---

#### Fix 2 — Context length exceeded: 421k tokens (model limit: 128k)
**Files**: `services/llm_orchestration_service.py`

**Problem**: Every chat call to the orchestrator was failing with:
```
LLM Chat Error: Error code: 400 - This model's maximum context length is 128000 tokens.
However, your messages resulted in 421175 tokens.
```

**Root cause**: `_service_snapshot()` included `extracted_knowledge` for up to 80 services with **no size limit**. After KB enrichment ran and populated each service's `extracted_knowledge` (potentially 10k–100k chars per service), the JSON payload sent as the user message exploded to 421k+ tokens.

Example: 80 services × 20k chars each = 1.6M chars ≈ 400k tokens.

**Fixes applied**:

1. **`_service_snapshot()` — `extracted_knowledge` cap: unlimited → 600 chars**
   This list is for routing/brief context only. The full knowledge is available separately when the service agent is dispatched.
   ```python
   # Before (no limit)
   "extracted_knowledge": (
       str(prompt_pack.get("extracted_knowledge") or "").strip()
       or extracted_kb_map.get(sid, "")
   ),

   # After (600 char cap)
   "extracted_knowledge": (
       str(prompt_pack.get("extracted_knowledge") or "").strip()
       or extracted_kb_map.get(sid, "")
   )[:600],
   ```

2. **`allowed_services_detailed` — `extracted_knowledge` cap: 8000 → 5000 chars**
   This is current-phase services only (typically 4–6 services), used by the orchestrator to answer information questions. 5000 chars per service is sufficient.
   ```python
   # Before
   "extracted_knowledge": str(row.get("extracted_knowledge") or "").strip()[:8000],

   # After
   "extracted_knowledge": str(row.get("extracted_knowledge") or "").strip()[:5000],
   ```

**Token budget after fixes** (approximate):

| Field | Before | After |
|---|---|---|
| `services` (80 svcs × extracted_knowledge) | ~400k tokens | ~12k tokens |
| `allowed_services_detailed` (~5 svcs) | ~10k tokens | ~6k tokens |
| `full_knowledge_base` (already capped) | ~20k tokens | ~20k tokens |
| system prompt + other fields | ~3k tokens | ~3k tokens |
| **Total** | **421k+ tokens** | **~41k tokens** |

**No functional regression**: The full `extracted_knowledge` per service is still passed in full when that service's agent is actually dispatched (`_run_service_agent` → `_build_service_grounding_pack`), so answer quality is preserved.

---

## Latest Update (March 5, 2026)

### Additive Integration Completed (No Replacement of Existing Flows)

- Added **Agent Plugin Runtime service**: `services/agent_plugin_service.py`
- Added **Menu OCR Plugin service**: `services/menu_ocr_plugin_service.py`
- Extended `config_service` with:
  - `agent_plugins` config model and defaults
  - plugin settings APIs (`enabled/shared_context/strict_mode/strict_unavailable_response`)
  - plugin CRUD methods
  - plugin fact workflow (add/update/approve/reject/delete)
  - `service_kb` record storage and lookup methods
- Extended admin API routes with:
  - `/api/config/agent-plugins/settings` (GET/PUT)
  - `/api/config/agent-plugins` (GET/POST)
  - `/api/config/agent-plugins/{plugin_id}` (GET/PUT/DELETE)
  - `/api/config/agent-plugins/clear-all` (DELETE)
  - `/api/config/agent-plugins/{plugin_id}/facts/*` (CRUD + approve/reject)
  - `/api/config/service-kb` and `/api/config/service-kb/record`
  - `/api/agent-builder/menu-ocr/status`
  - `/api/agent-builder/menu-ocr/scan`
  - `/api/agent-builder/menu-ocr/logs`
  - `/api/agent-builder/menu-ocr/logs/{run_id}`
- Integrated plugin runtime into `chat_service` behind safe guard:
  - `AGENT_PLUGIN_RUNTIME_ENABLED` (new env setting, default `false`)
  - plugin runtime also requires admin config `agent_plugins.enabled = true`
- Added config/env settings:
  - `config/settings.py`: `agent_plugin_runtime_enabled`
  - `.env.example`: `AGENT_PLUGIN_RUNTIME_ENABLED=false`

### Safety / Compatibility Notes

- Existing **phase + ticketing** flow remains unchanged by default.
- Plugin runtime is **off by default** at environment level.
- Integration is additive and backward-compatible with existing JSON config.

### Validation Snapshot

- `tests/test_config_service.py` + `tests/test_policy_and_service_runtime.py`: **56 passed**

### Conversation Audit Logging Added (March 5, 2026)

- Added new service: `services/conversation_audit_service.py`
- Integrated chat-turn logging in `api/routes/chat.py` for:
  - successful responses
  - DB fallback responses
  - failed turns
- Added new settings:
  - `conversation_audit_enabled` (`CONVERSATION_AUDIT_ENABLED`, default `true`)
  - `conversation_audit_log_file` (`CONVERSATION_AUDIT_LOG_FILE`, default `./logs/conversation_audit.jsonl`)
- New single log file captures per turn:
  - user message + bot message
  - phase info (requested/resolved/phase-gate fields)
  - enabled services snapshot (overall + phase-specific)
  - ticketing outcomes/details from response metadata
  - routing, trace, channel, session, and error/db-fallback details

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [PRD Requirements Summary](#2-prd-requirements-summary)
3. [Current Lumira Architecture](#3-current-lumira-architecture)
4. [Problems in Lumira (12 Issues)](#4-problems-in-lumira-12-issues)
5. [Real Failure Cases (Screenshots)](#5-real-failure-cases-screenshots)
6. [New Bot Requirements](#6-new-bot-requirements)
7. [Architecture Recommendations](#7-architecture-recommendations)
8. [Implementation Checklist](#8-implementation-checklist)
9. [Development Log](#9-development-log)

---

## 1. Project Overview

### What is Lumira?
Lumira is KePSLA's current AI-powered hotel guest service platform with 4 components:

| Component | Purpose | Channel |
|-----------|---------|---------|
| **Guest Journey Bot** | WhatsApp concierge for hotel guests | WhatsApp |
| **Engage Bot** | Website chatbot for pre-booking inquiries | Web Widget |
| **Synopsis** | Automated guest review analysis | Backend |
| **Ticketing System** | Service request creation and routing | Internal |

### Current Tech Stack
- **Backend**: Flask (Python 3.11+)
- **LLMs**: OpenAI GPT-4.1, GPT-4.1-mini, GPT-5-nano
- **Vector Search**: FAISS
- **Database**: MySQL + Redis
- **Storage**: AWS S3
- **Task Queue**: Celery

### Why Build a New Bot?
Lumira has fundamental architectural flaws that cause:
- Contradictory responses
- Lost conversation context
- Duplicate tickets
- No human escalation
- No confidence tracking

A rewrite with proper architecture is more efficient than patching.

---

## 2. PRD Requirements Summary

### Product Vision
> KePSLA Conversational AI Platform is an **industry-agnostic, configurable** AI-powered conversational layer designed to act as a **single custodial interface** for businesses.

### Core Design Principles (MUST FOLLOW)

| # | Principle | What It Means |
|---|-----------|---------------|
| 1 | **Intent-first, not industry-first** | Bot logic driven by user intent, not hardcoded flows |
| 2 | **Confidence-aware responses** | AI confidence determines continuation OR escalation |
| 3 | **Human-in-the-loop by design** | Human takeover is a safety mechanism, not failure |
| 4 | **Configurable, not hardcoded** | All flows, messages, rules are admin-configurable |
| 5 | **One platform, multiple use cases** | Same architecture supports multiple industries |

### Supported Use Cases

| Use Case | Description |
|----------|-------------|
| FAQ & Information | Product, service, or general queries |
| Lead Capture | Sales inquiries, demo requests, pricing |
| Complaint / Support | Issue reporting and ticket creation |
| Callback Request | Requests for phone or follow-up calls |
| Human Escalation | Live agent or offline escalation |

### Escalation Triggers (CRITICAL)
The bot MUST escalate when:
1. User explicitly requests to talk to a human
2. AI confidence score below defined threshold
3. Repeated clarification loops (user asks same thing multiple times)
4. Manual agent takeover

### Escalation Modes
- Live chat (if available)
- Support ticket creation
- Email follow-up
- Callback scheduling

### Data Requirements

| Workflow | Mandatory Fields |
|----------|------------------|
| Lead / Demo | Name, Email, Phone, Organization, Designation |
| Complaint | Issue Description, Date/Time |
| Callback | Preferred Time, Agenda |

### Non-Functional Requirements
- Mobile responsive
- Fast load times (<1 second)
- Secure (HTTPS, GDPR-compliant)
- Role-based access
- Scalable architecture

---

## 3. Current Lumira Architecture

### Request Flow (Simplified)
```
User Message
    │
    ▼
┌─────────────────┐
│  Scope Guard    │ ← Checks if request is in-scope (NO guest context!)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Menu Parser    │ ← Checks if it's a menu request (returns SINGLE URL)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Message Parser  │ ← Extracts intent, creates ticket (HAS guest context)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Ticket Creator  │ ← Creates ticket via Kepsla API
└─────────────────┘
```

### Key Files (Lumira Develop Branch)
```
lumira/
├── lumira.py                          # Main Flask app
├── app/
│   ├── guest_journey/
│   │   ├── api/guest_journey_bot_api.py   # Main endpoint
│   │   └── llm_engine/
│   │       ├── parse_guest_message.py     # Message parsing
│   │       └── hotel_prompt_engine.py     # Prompt generation
│   ├── engage/
│   │   ├── llm_engine/
│   │   │   ├── prompt_engine.py
│   │   │   └── query_rewriter.py
│   │   └── utils/
│   │       ├── scope_guard.py             # Scope checking
│   │       └── rag_pipeline.py            # FAISS search
│   └── ticketing_system/
│       └── services/
│           ├── ticket_creator.py
│           ├── ticket_update.py
│           └── tool_dispatcher.py
```

### What's Missing in Lumira
| Component | Status |
|-----------|--------|
| Conversation State Machine | NOT IMPLEMENTED |
| Capability Registry | NOT IMPLEMENTED |
| Confidence Scoring | NOT IMPLEMENTED |
| Response Validation | NOT IMPLEMENTED |
| Human Escalation (hotfix) | NOT IMPLEMENTED |
| Multi-menu Support | NOT IMPLEMENTED |
| Confirmation Handling | NOT IMPLEMENTED |

---

## 4. Problems in Lumira (12 Issues)

### Category A: Architecture Problems

#### Problem A: No Conversation State Machine
**Current**: Each message processed independently
**Impact**: Bot forgets context between messages

```python
# CURRENT (Bad)
def process_message(message):
    # No state tracking
    response = llm.generate(message)
    return response

# SHOULD BE
class ConversationState:
    current_intent: str
    entities_collected: dict
    pending_confirmation: Optional[PendingAction]
    open_tickets: list
    escalation_requested: bool
```

**Real Failure**: User says "burger" → "room" → Bot forgets burger, asks about housekeeping

---

#### Problem B: No Intent Verification Layer
**Current**: Scope Guard and Message Parser can contradict each other
**Impact**: Bot says "I can help" then "I can't help"

```python
# CURRENT (Bad)
scope_result = check_scope(message)      # Says IN_SCOPE
parsed = parse_message(message)          # Says OUT_OF_SCOPE
# No verification between them!

# SHOULD BE
def process_with_verification(message):
    intent = classify_intent(message)
    if not verify_capability(intent):
        return escalate_or_decline(intent)
    return execute_intent(intent)
```

---

#### Problem C: No Capability Registry
**Current**: LLM "guesses" what hotel can do based on vague prompts
**Impact**: Bot promises things hotel can't deliver

```python
# SHOULD HAVE
HOTEL_CAPABILITIES = {
    "room_service": {
        "enabled": True,
        "hours": "24/7",
        "delivery_from": ["IRD"],  # NOT Kadak, Aviator
    },
    "restaurant_delivery_to_room": {
        "enabled": False,  # Kadak/Aviator are dine-in only
    },
    "cab_service": {
        "enabled": True,
        "types": ["airport_transfer", "local"],
        "intercity": False,  # NO Hyderabad trips
    },
    "multiple_menu_request": {
        "enabled": True,
        "max_menus": 3,
    }
}
```

**Real Failure**: Bot offers to arrange cab to Hyderabad, hotel is in Mumbai

---

#### Problem D: No Response Validation Layer
**Current**: LLM response goes directly to user
**Impact**: Contradictory responses sent to users

```python
# CURRENT (Bad)
response = llm.generate(prompt)
return response  # No validation!

# SHOULD BE
response = llm.generate(prompt)
validated = validate_response(response, hotel_capabilities, conversation_state)
if validated.has_contradiction:
    response = regenerate_with_correction(validated.issues)
return response
```

**Real Failure**: "I'll check nearby restaurants" → "I can't check nearby restaurants"

---

### Category B: Technical Problems

#### Problem E: Conversation History Too Short
**Current**: Only fetches last 10 messages
**Impact**: Context lost in longer conversations

```python
# CURRENT
messages = fetch_last_n_messages(wa_number, entity_id, n=10)

# SHOULD BE
messages = fetch_last_n_messages(n=10)
if conversation_is_ongoing:
    summary = get_conversation_summary(session_id)
    context = summary + messages
```

---

#### Problem F: Ticket Matching Logic is Weak
**Current**: Simple text matching for ticket updates
**Impact**: Duplicate tickets created for same request

```python
# SHOULD BE
def should_update_ticket(new_message, open_tickets, conversation):
    # 1. Semantic similarity check (embeddings)
    similar_tickets = find_semantically_similar(new_message, open_tickets)

    # 2. Time-based check (within 30 min = same conversation)
    recent_tickets = filter_by_time(similar_tickets, minutes=30)

    # 3. Entity matching (same room, same department)
    matching_tickets = filter_by_entities(recent_tickets, conversation)

    if matching_tickets:
        return "UPDATE", matching_tickets[0]
    return "CREATE", None
```

**Real Failure**: Ticket 5090 "Reserve table at Aviator" + Ticket 5091 "Reserve table at Aviator for 5 guests at 7:30 PM" (should be ONE ticket)

---

#### Problem G: Menu Parser Returns Single URL
**Current**: Can only send one menu at a time
**Impact**: User asks for 3 menus, gets 1

```python
# CURRENT (Bad)
class ParsedMenuRequest(BaseModel):
    is_menu_request: bool
    media: str | None      # SINGLE
    url: str | None        # SINGLE

# SHOULD BE
class ParsedMenuRequest(BaseModel):
    is_menu_request: bool
    menus: list[MenuResponse]  # MULTIPLE

class MenuResponse(BaseModel):
    name: str
    media_type: str
    url: str
```

**Real Failure**: "I want all 3 menus" → Bot sends only Kadak menu

---

#### Problem H: "Yes/No" Confirmation Not Handled
**Current**: No confirmation state tracking
**Impact**: User says "yes", bot asks the same question again

```python
# SHOULD HAVE
class ConversationState:
    pending_confirmation: Optional[PendingAction] = None

class PendingAction:
    action_type: str  # "order", "booking", "ticket"
    details: dict
    asked_at: datetime

# When user says "yes"
if state.pending_confirmation and is_affirmative(message):
    execute_pending_action(state.pending_confirmation)
```

**Real Failure**: Bot asks "Would you like to proceed?" → User says "yes" → Bot asks "Which sandwich would you like?"

---

#### Problem I: Guest Context Not Passed Consistently
**Current**: Scope Guard has NO guest context, Message Parser HAS guest context
**Impact**: Bot knows name in one response, forgets in next

```python
# CURRENT (Bad)
check_scope_llm(full_context, phase)           # NO guest context
parse_guest_message_v2(conversation, guest_context, ...)  # HAS guest context

# SHOULD BE - ALL LLM calls get same context
llm_context = {
    "guest": guest_context,
    "conversation": full_context,
    "capabilities": hotel_capabilities,
    "open_tickets": open_tickets,
}

check_scope_llm(llm_context)
parse_menu_request(llm_context)
parse_guest_message(llm_context)
```

**Real Failure**: "Your name is Sana" → "I can't access personal information like your name" (same conversation!)

---

#### Problem J: No Fallback Hierarchy
**Current**: If not in scope, returns generic decline
**Impact**: Dead ends instead of graceful degradation

```python
# CURRENT (Bad)
if not in_scope:
    return "I can't help with that"

# SHOULD BE
FALLBACK_HIERARCHY = [
    ("try_rag_search", "Search knowledge base"),
    ("try_web_search", "Search external sources"),
    ("create_ticket", "Create ticket for human follow-up"),
    ("escalate_human", "Connect to human agent"),
    ("polite_decline", "Apologize and offer alternatives"),
]

for fallback, description in FALLBACK_HIERARCHY:
    result = try_fallback(fallback, message)
    if result.success:
        return result
```

---

### Category C: Prompt Engineering Problems

#### Problem K: Contradictory System Prompts
**Current**: Different prompts have different personalities/capabilities

```
Scope Guard Prompt: "Help with transport, concierge desk..."
Message Parser Prompt: "You are Zack the concierge..."
Menu Parser Prompt: "You are Zack for ICONIQA Hotel..."
```

**Should Be**: Single source of truth

```python
BASE_SYSTEM_PROMPT = """
You are {bot_name}, the AI concierge for {hotel_name}.

CAPABILITIES (what you CAN do):
{capabilities_list}

LIMITATIONS (what you CANNOT do):
{limitations_list}

GUEST CONTEXT:
{guest_context}

CONVERSATION RULES:
{rules}
"""
# All prompts extend this base
```

---

#### Problem L: No Few-Shot Examples
**Current**: Prompts describe behavior but don't show examples
**Impact**: LLM doesn't know correct behavior patterns

```
# SHOULD INCLUDE
Example 1:
User: "I want all 3 menus"
Correct: [Send all 3 menu URLs]
Incorrect: "I can only send one at a time"

Example 2:
User: "yes"
Context: Bot just asked "Would you like to confirm this order?"
Correct: Confirm the order
Incorrect: "What would you like to order?"
```

---

## 5. Real Failure Cases (Screenshots)

### Case 1: Kadak Delivery Contradiction
**Conversation**:
- User: "i want items from kadak delivered to my room"
- Bot: Sends Kadak menu, asks "What would you like to have delivered to your room?"
- User: "so kadak item i want it inroom"
- Bot: "Kadak menu items are available for dine-in at the restaurant, but not for In-Room Dining delivery"

**Problem**: Bot implied delivery was possible, then said it wasn't
**Root Cause**: Problem C (No Capability Registry)

---

### Case 2: Multiple Menus - Only One Sent
**Conversation**:
- User: "i want 3 menus, dine in menu, kadak menu and avioator menu"
- Bot: Explains which menus are for what
- User: "i want all 3 menus to decide"
- Bot: Sends ONLY Kadak menu

**Problem**: User explicitly asked for 3 menus, got 1
**Root Cause**: Problem G (Single Menu URL only)

---

### Case 3: Hyderabad Cab from Mumbai Hotel
**Conversation**:
- User: "i want a cab to take me around hyderbad"
- Bot: "Our team will help arrange a cab for your travel in Hyderabad"
- User: "is ur hotel mumbai based on hyd"
- Bot: "ICONIQA is based in Mumbai, near Terminal 2"
- User: "how will ur cab take me to hyd"
- Bot: "We don't offer intercity cab services to Hyderabad"

**Problem**: Bot agreed to arrange cab to Hyderabad, then admitted hotel is in Mumbai
**Root Cause**: Problem C (No Capability Registry), Problem D (No Response Validation)

---

### Case 4: "I'll Check" → "I Can't Check"
**Conversation**:
- User asks for mac and cheese nearby
- Bot: "I'll have our team check for places nearby ICONIQA..."
- User: "can you not check"
- Bot: "I don't have access to outside restaurant info, so I can't check nearby places directly"

**Problem**: Bot said it would check, then said it can't
**Root Cause**: Problem D (No Response Validation)

---

### Case 5: Can't Send Multiple Menus
**Conversation**:
- User: "I SAID ENTIRE MENU BRO STOP PISSING ME OFF"
- User: "idk give all three"
- User: "ALL THREE MENUS"
- Bot: "I can only provide one menu at a time. Could you please choose one"

**Problem**: System limitation frustrates user
**Root Cause**: Problem G (Single Menu URL only)

---

### Case 6: Context Lost (Burger in Room)
**Conversation**:
- User: "burger"
- Bot: "Could you please specify if you'd like a burger from In-Room Dining or restaurant?"
- User: "room"
- Bot: "Could you clarify what you need for your room? Housekeeping, amenities?"
- User: "I SAID BURGER IN ROOM MAD OR WHAT"
- Bot: Finally shows burger options

**Problem**: Bot forgot "burger" when user said "room"
**Root Cause**: Problem A (No State Machine), Problem E (History too short)

---

### Case 7: Duplicate Tickets
**Evidence**: Ticket system shows:
- Ticket 5090: "REQUEST TO RESERVE A TABLE AT AVIATOR" (3:00 PM)
- Ticket 5091: "REQUEST TO RESERVE A TABLE AT AVIATOR FOR 5 GUESTS AT 7:30 PM" (3:01 PM)

**Problem**: Should have updated ticket 5090 with guest count/time
**Root Cause**: Problem F (Weak Ticket Matching)

---

### Case 8: "Yes" Confirmation Ignored
**Conversation**:
- User: Complex order request
- Bot: "Would you like to proceed with Iconiqa Club, assorted fries, and Coke?"
- User: "yes"
- Bot: "Could you please specify which sandwich you'd like from our menu?"

**Problem**: User confirmed, bot asked again
**Root Cause**: Problem H (No Confirmation Handling)

---

### Case 9: Name Contradiction
**Conversation**:
- User: "what is my name"
- Bot: "Your name is Sana. If you need any more help..."
- Bot: "I can't access personal information like your name."

**Problem**: Two consecutive messages contradict each other
**Root Cause**: Problem I (Inconsistent Guest Context)

---

## 6. New Bot Requirements

### Must Have (P0)

| Requirement | Description |
|-------------|-------------|
| Conversation State Machine | Track intent, entities, pending confirmations across messages |
| Capability Registry | Explicit, configurable list of what bot can/cannot do |
| Response Validation | Check responses against capabilities before sending |
| Confidence Scoring | Track AI confidence, escalate when low |
| Human Escalation | Multiple modes: live chat, ticket, callback |
| Multi-item Support | Send multiple menus, handle multiple requests |
| Confirmation Handling | Track and execute pending confirmations |
| Consistent Context | All LLM calls receive same guest/conversation context |

### Should Have (P1)

| Requirement | Description |
|-------------|-------------|
| Conversation Summarization | Summarize long conversations for context |
| Semantic Ticket Matching | Use embeddings to find related tickets |
| Fallback Hierarchy | Graceful degradation with multiple fallback options |
| Few-shot Examples | Include examples in prompts for correct behavior |
| Loop Detection | Detect repeated clarification requests, auto-escalate |

### Nice to Have (P2)

| Requirement | Description |
|-------------|-------------|
| Admin Configuration UI | Configure capabilities, thresholds via dashboard |
| Analytics Dashboard | Track confidence scores, escalation rates |
| A/B Testing | Test different prompts/flows |

---

## 7. Architecture Recommendations

### Proposed Architecture

```
User Message
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│                 UNIFIED CONTEXT BUILDER                  │
│  - Load guest context                                   │
│  - Load conversation history + summary                  │
│  - Load open tickets                                    │
│  - Load hotel capabilities                              │
└─────────────────────┬───────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────┐
│                 CONVERSATION STATE MACHINE               │
│                                                         │
│  States: IDLE → COLLECTING_INFO → CONFIRMING → DONE    │
│                                                         │
│  Tracks:                                                │
│  - current_intent                                       │
│  - entities_collected                                   │
│  - pending_confirmation                                 │
│  - confidence_score                                     │
└─────────────────────┬───────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────┐
│                 INTENT CLASSIFIER                        │
│                                                         │
│  1. Classify intent (with confidence score)             │
│  2. Check against CAPABILITY_REGISTRY                   │
│  3. If not capable → FALLBACK_HANDLER                   │
│  4. If capable → INTENT_EXECUTOR                        │
└─────────────────────┬───────────────────────────────────┘
                      │
          ┌───────────┴───────────┐
          │                       │
          ▼                       ▼
┌─────────────────┐     ┌─────────────────┐
│ INTENT_EXECUTOR │     │ FALLBACK_HANDLER│
│                 │     │                 │
│ - Menu request  │     │ 1. RAG search   │
│ - Room service  │     │ 2. Create ticket│
│ - Booking       │     │ 3. Escalate     │
│ - Complaint     │     │ 4. Decline      │
└────────┬────────┘     └────────┬────────┘
         │                       │
         └───────────┬───────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│                 RESPONSE VALIDATOR                       │
│                                                         │
│  1. Check response against capabilities                 │
│  2. Check for contradictions with history               │
│  3. If issues → regenerate                              │
│  4. If valid → return to user                           │
└─────────────────────────────────────────────────────────┘
```

### Key Components to Build

```
new_bot/
├── main.py                        # Entry point
├── config/
│   ├── capabilities.py            # Capability Registry
│   ├── prompts.py                 # Unified prompt templates
│   └── settings.py                # Configuration
├── core/
│   ├── context_builder.py         # Build unified context
│   ├── state_machine.py           # Conversation state tracking
│   ├── intent_classifier.py       # Intent classification + confidence
│   └── response_validator.py      # Validate before sending
├── handlers/
│   ├── menu_handler.py            # Multi-menu support
│   ├── order_handler.py           # Order with confirmation
│   ├── booking_handler.py         # Table/room booking
│   ├── ticket_handler.py          # Smart ticket create/update
│   └── fallback_handler.py        # Graceful fallbacks
├── llm/
│   ├── client.py                  # LLM client wrapper
│   ├── prompts/                   # Prompt templates
│   └── parsers.py                 # Structured output parsing
├── integrations/
│   ├── whatsapp.py                # WhatsApp integration
│   ├── ticketing.py               # Kepsla ticket API
│   └── database.py                # MySQL/Redis
└── utils/
    ├── confidence.py              # Confidence scoring
    ├── similarity.py              # Semantic similarity
    └── escalation.py              # Human handoff logic
```

---

## 8. Implementation Checklist

### Phase 1: Core Architecture
- [ ] Set up project structure
- [ ] Implement Capability Registry
- [ ] Implement Conversation State Machine
- [ ] Implement Unified Context Builder
- [ ] Create base prompt templates

### Phase 2: Intent & Response
- [ ] Implement Intent Classifier with confidence
- [ ] Implement Response Validator
- [ ] Implement Fallback Handler
- [ ] Add few-shot examples to prompts

### Phase 3: Handlers
- [ ] Menu Handler (multi-menu support)
- [ ] Order Handler (with confirmation tracking)
- [ ] Booking Handler
- [ ] Ticket Handler (smart create/update)

### Phase 4: Integrations
- [ ] WhatsApp integration
- [ ] Kepsla Ticket API integration
- [ ] Database setup (MySQL + Redis)

### Phase 5: Testing & Validation
- [ ] Test all 9 failure cases from Lumira
- [ ] Load testing
- [ ] UAT with hotel staff

---

## 9. Development Log

### February 3, 2026
**Status**: Planning Complete
**What was done**:
- Analyzed Lumira documentation (DEVELOP and HOTFIX branches)
- Reviewed PRD requirements
- Identified 12 core problems in Lumira
- Documented 9 real failure cases from testing
- Created architecture recommendations
- Created implementation checklist

**Files reviewed**:
- LUMIRA_DEVELOP_DOCUMENTATION.md
- LUMIRA_HOTFIX_DOCUMENTATION.md
- KePSLA Conversational AI Platform - Global.docx (PRD)
- TABLE OF CONTENTS.pdf (Failure screenshots)

**Next steps**:
- Decide on tech stack for new bot
- Set up project repository
- Start Phase 1 implementation

---

### February 3, 2026 (Update 2)
**Status**: Planning Documents Complete
**What was done**:
- Created comprehensive planning documentation in `/docs` folder
- Generated 5 detailed documents covering all aspects of new bot development

**Documents Created**:

| Document | Description | Size |
|----------|-------------|------|
| `01_RESEARCH_ANALYSIS.md` | Gap analysis, problem categorization, recommendations | ~6KB |
| `02_PROJECT_PLAN.md` | 10-week phased plan, task breakdown, milestones | ~15KB |
| `03_TECH_STACK.md` | Technology choices with justifications, cost estimates | ~12KB |
| `04_ARCHITECTURE.md` | System architecture, components, data flow, schemas | ~25KB |
| `05_LLM_DESIGN.md` | LLM strategy, prompts, confidence scoring, RAG | ~30KB |

**Key Decisions Made**:
- **Backend**: FastAPI (over Flask) - better async, validation
- **Database**: PostgreSQL (over MySQL) - better JSON support
- **LLM Strategy**: Multi-model routing (gpt-4.1-mini default, gpt-4.1 for complex)
- **Vector DB**: FAISS for dev, Pinecone for production scale
- **Architecture**: ~~Modular monolith with state machine~~ **HYBRID APPROACH**

**Next steps**:
- [ ] Review documents with team
- [ ] Get stakeholder approval
- [ ] Set up project repository
- [ ] Start Phase 1: Core Architecture

---

### February 3, 2026 (Update 3)
**Status**: Architecture Updated to Hybrid Approach
**What was done**:
- Changed architecture from Single Pipeline to **Hybrid (Fast Pipeline + Agent Team)**
- Updated `04_ARCHITECTURE.md` with complete hybrid design

**Architecture Decision: HYBRID**

```
┌─────────────────────────────────────────────────────────────────┐
│                    MESSAGE ROUTING                               │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ████████████████████████████████████░░░░░░░                   │
│  │◄────── SIMPLE (85-90%) ─────────▶│◀─ COMPLEX (10-15%) ─▶│   │
│                                                                 │
│  FAST PIPELINE                       AGENT TEAM                 │
│  • 3-4 LLM calls                    • 6-8 LLM calls            │
│  • ~1.6 seconds                     • ~3-4 seconds             │
│  • ~$0.003/message                  • ~$0.012/message          │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**Why Hybrid?**
- 90% of messages are simple → Use fast, cheap pipeline
- 10% of messages are complex (all Lumira failures were here) → Use thorough agent team
- Best of both worlds: speed for simple, quality for complex

**Key Components Added**:
1. **Complexity Router** - Decides SIMPLE vs COMPLEX path
2. **Fast Pipeline** - Intent → Capability Check → Handler → Response
3. **Agent Team** - Orchestrator → Research/Planner/Executor → Synthesizer
4. **Response Validator** - Shared by both paths

**Lumira Failures Handled**:
| Single Pipeline | Hybrid |
|-----------------|--------|
| 3/9 failures fixed | 9/9 failures fixed |

**Next steps**:
- [ ] Review hybrid architecture with team
- [ ] Finalize complexity router thresholds
- [ ] Start implementation

---

## Quick Reference: Problem → Solution Map

| Problem | Solution |
|---------|----------|
| A: No state machine | Implement ConversationState class |
| B: No intent verification | Add capability check after classification |
| C: No capability registry | Create HOTEL_CAPABILITIES config |
| D: No response validation | Add ResponseValidator before send |
| E: History too short | Add conversation summarization |
| F: Weak ticket matching | Use embeddings for semantic matching |
| G: Single menu only | Support list[MenuResponse] |
| H: No confirmation handling | Track pending_confirmation in state |
| I: Inconsistent context | Unified context for all LLM calls |
| J: No fallback hierarchy | Implement FALLBACK_HIERARCHY |
| K: Contradictory prompts | Single BASE_SYSTEM_PROMPT |
| L: No few-shot examples | Add examples to all prompts |

---

### February 4, 2026
**Status**: Implementation Started - Project Setup Complete
**What was done**:
- Created task division document for teammate (`TEAMMATE_TASKS.md`)
- Set up complete project structure for `new_bot/`
- Implemented core foundation files
- Created test UI for bot interaction

**Task Division**:
| Teammate (Easy Tasks) | Lead (Complex Tasks) |
|----------------------|---------------------|
| OCR Module | State Machine & Context |
| Database Models | Intent Classifier |
| Pydantic Schemas | LLM Integration |
| Menu CRUD Service | Response Validator |
| Text Utilities | Capability Registry |
| Unit Tests | Agent Orchestration |

**Files Created**:
```
new_bot/
├── requirements.txt           # All dependencies
├── .env.example              # Environment template
├── main.py                   # FastAPI entry point
├── config/
│   ├── __init__.py
│   └── settings.py           # Pydantic settings
├── core/
│   ├── __init__.py
│   ├── context_manager.py    # Session & context management
│   └── state_machine.py      # Conversation state transitions
├── schemas/
│   ├── __init__.py
│   └── chat.py               # Chat request/response models
├── services/
│   ├── __init__.py
│   └── chat_service.py       # Mock chat service (for testing)
├── api/routes/
│   ├── __init__.py
│   └── chat.py               # Chat API endpoints
├── templates/
│   └── chat.html             # Test UI
└── static/
    ├── css/chat.css          # UI styling
    └── js/chat.js            # Chat functionality
```

**Test UI Features**:
- Real-time chat interface
- Session management (new session, reset state)
- Debug panel (Ctrl+D to toggle)
- Quick test buttons for common scenarios
- Shows intent, confidence, and state
- Conversation history viewer

**To Run the Bot**:
```bash
cd new_bot
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your OpenAI key
python main.py
# Open http://localhost:8000
```

**Next steps**:
- [ ] Implement actual LLM client (replace mock)
- [ ] Build capability registry
- [ ] Implement intent classifier with confidence scoring
- [ ] Build response validator
- [ ] Connect to database (PostgreSQL)
- [ ] Connect to Redis for session persistence

---

### February 4, 2026 (Update 2)
**Status**: Server Running Successfully
**What was done**:
- Fixed requirements.txt (removed packages with Windows build issues)
- Simplified dependencies for development:
  - SQLite instead of PostgreSQL
  - In-memory sessions instead of Redis
  - Commented out heavy packages (FAISS, langchain, easyocr)
- Added missing jinja2 dependency
- Server now runs at http://localhost:8000

**Requirements Changes**:
```
Removed/Commented (Windows build issues):
- psycopg2-binary (needs PostgreSQL installed)
- asyncpg (needs PostgreSQL)
- faiss-cpu (needs C++ build tools)
- easyocr (heavy, teammate will install)
- aiohttp (build issues)
- tiktoken (build issues)
- redis/hiredis (not needed for dev)

Added:
- jinja2 (for HTML templates)
- aiosqlite (SQLite async support)
```

**Verified Working**:
- [x] Server starts without errors
- [x] Test UI loads at http://localhost:8000
- [x] Chat API responds to messages
- [x] Mock responses working (greeting, menu, order flow)
- [x] Session state tracking works
- [x] Quick test buttons functional

**Next steps**:
- [x] Implement actual LLM client (connect to OpenAI) ✅
- [ ] Build capability registry
- [ ] Implement intent classifier with confidence scoring
- [ ] Build response validator

---

### February 4, 2026 (Update 3)
**Status**: OpenAI Integration Complete
**What was done**:
- Created LLM client (`llm/client.py`) with OpenAI integration
- Updated chat service to use real LLM instead of mock responses
- Implemented intent classification with confidence scoring
- Implemented response generation with context awareness

**Files Created/Modified**:
```
new_bot/
├── llm/
│   ├── __init__.py          # Updated exports
│   └── client.py            # NEW - OpenAI client wrapper
└── services/
    └── chat_service.py      # UPDATED - Uses real LLM
```

**LLM Client Features**:
- `chat()` - Basic chat completion
- `chat_with_json()` - JSON response format
- `classify_intent()` - Intent classification with confidence
- `generate_response()` - Context-aware response generation

**Intent Classification**:
- Uses GPT for intent classification
- Returns: intent, confidence (0-1), entities, reasoning
- Supports 13 intent types (greeting, menu_request, order_food, etc.)
- Falls back to "unclear" on errors

**Response Generation**:
- Context-aware (hotel, guest, room, conversation history)
- State-aware (respects pending confirmations)
- Follows hotel concierge persona "Zack"

**Flow**:
```
User Message
    ↓
Classify Intent (LLM) → confidence score
    ↓
Validate State (state machine)
    ↓
Generate Response (LLM)
    ↓
Update State
    ↓
Return Response
```

**Next steps**:
- [x] Test with real conversations ✅
- [ ] Build capability registry (define what bot can/cannot do)
- [ ] Add response validation layer
- [ ] Add error handling improvements

---

### February 4, 2026 (Update 4)
**Status**: Basic Chat Flow Working End-to-End
**What was done**:
- Fixed `IntentType.COMPLETED` bug (was using intent instead of state)
- Added better error handling in LLM client (returns fallback instead of raising)
- Added traceback logging for debugging
- Tested full conversation flow successfully

**Verified Working Flow**:
```
User: "hi"
Bot: Welcome greeting (intent: greeting, confidence: 100%)

User: "I want to order food"
Bot: Shows menu options (intent: order_food, confidence: 95%)

User: "I want to order pizza"
Bot: Confirms pizza choice, asks for confirmation (intent: order_food)

User: "Yes, confirm"
Bot: Order confirmed with Order ID (intent: confirmation_yes)
```

**Key Fixes**:
- `chat_service.py:255` - Changed `IntentType.COMPLETED` to `ConversationState.COMPLETED`
- `llm/client.py` - Returns fallback response instead of raising on errors
- `api/routes/chat.py` - Added traceback logging for debugging

**Next steps**:
- [ ] Build capability registry (prevent bot from promising unavailable services)
- [ ] Add response validation layer
- [ ] Test edge cases (multi-menu, complaints, escalation)
- [ ] Connect to database for persistence

---

### February 4, 2026 (Update 5)
**Status**: Hybrid Approach Added to Plan
**What was done**:
- Analyzed existing RAG plan in `05_LLM_DESIGN.md`
- Identified gap: RAG alone doesn't prevent hallucination for structured data
- Designed enhanced **Hybrid Approach** (Structured + RAG + LLM)

**Gap Analysis**:
| Component | In Original Plan? | Problem Without It |
|-----------|-------------------|-------------------|
| RAG Pipeline | ✅ Yes | - |
| Capability Registry | ✅ Yes | - |
| Response Validator | ✅ Yes | - |
| **Structured Menu Data** | ❌ No | LLM invents menu items/prices |
| **Structured Availability** | ❌ No | LLM guesses delivery options |
| **Data-Grounded Generation** | ❌ No | LLM can still hallucinate |

**Enhanced Hybrid Architecture**:
```
User Message
    │
    ▼
┌─────────────────────────────────────────┐
│         INTENT CLASSIFIER (LLM)         │
└─────────────────┬───────────────────────┘
                  │
    ┌─────────────┼─────────────┐
    ▼             ▼             ▼
┌────────┐  ┌──────────┐  ┌──────────┐
│STRUCTURED│  │   RAG    │  │   LLM    │
│  DATA   │  │ SEARCH   │  │  ONLY    │
├─────────┤  ├──────────┤  ├──────────┤
│• Menus  │  │• FAQs    │  │• Greeting│
│• Prices │  │• Policies│  │• Chitchat│
│• Hours  │  │• General │  │• Unclear │
│• Caps   │  │  info    │  │          │
└────┬────┘  └────┬─────┘  └────┬─────┘
     └─────────┬──┴─────────────┘
               ▼
┌─────────────────────────────────────────┐
│      RESPONSE GENERATOR (LLM)           │
│  "Format using ONLY provided data"      │
└─────────────────┬───────────────────────┘
                  ▼
┌─────────────────────────────────────────┐
│         RESPONSE VALIDATOR              │
│  "Does response match source data?"     │
└─────────────────────────────────────────┘
```

**Data Source Routing**:
| Intent | Data Source | LLM Role |
|--------|-------------|----------|
| menu_request | Menu DB (exact) | Format only |
| order_food | Menu DB (validate) | Confirm items exist |
| faq | RAG (retrieval) | Summarize retrieved |
| greeting | None | Full generation |
| capability_check | Registry (exact) | Format only |
| complaint | None + Ticket DB | Create ticket |

**Implementation Order** (before Hybrid RAG):
1. ✅ Basic chat flow - DONE
2. ✅ OpenAI integration - DONE
3. ⬜ **Capability Registry** - Define what bot can/cannot do
4. ⬜ **Database Models** - Menu, Order, Guest tables
5. ⬜ **Response Validator** - Check before sending
6. ⬜ **Data Service** - Query structured data
7. ⬜ **RAG Pipeline** - For FAQs/policies
8. ⬜ **Hybrid Router** - Route to correct data source

**Next steps** (in order):
1. [ ] Build Capability Registry (config/capabilities.py)
2. [ ] Create Database Models (teammate task, but define schema)
3. [ ] Build Response Validator
4. [ ] Create Data Service for structured queries
5. [ ] Implement RAG Pipeline
6. [ ] Integrate Hybrid Router

---

### February 4, 2026 (Update 6)
**Status**: Lumira Reference Files Explored
**What was done**:
- Extracted and analyzed both Lumira zip files
- Identified key reference files for implementation

**Lumira Reference Files** (for future implementation):

| File | Purpose | Use For |
|------|---------|---------|
| `guest_journey/llm_engine/base_prompt.txt` | Main bot prompt (197 lines) | Prompt engineering reference |
| `guest_journey/llm_engine/parse_guest_message.py` | Message parsing with context | LLM integration patterns |
| `ticketing_system/models/structured_response.py` | ParsedIssue, ParsedMenuRequest | Response schema design |
| `extensions/db/common_models.py` | GuestInfo model | Database schema reference |
| `engage/utils/scope_guard.py` | Scope classification | Capability checking logic |
| `engage/utils/rag_pipeline.py` | FAISS RAG implementation | RAG pipeline reference |
| `engage/llm_engine/prompt_engine.py` | Prompt templates | Prompt design |
| `config.py` | Environment config | Settings structure |

**Key Learnings from Lumira**:

1. **Prompt Structure** (`base_prompt.txt`):
   - Ticket creation rules (duplicate detection)
   - Menu request handling
   - In-room dining validation
   - Spa/dining reservation rules
   - Phase-based validation (Booking, Pre-check-in, During Stay)
   - Time validation (no past bookings)

2. **Data Structures** (`structured_response.py`):
   ```python
   ParsedIssue:
     - is_ticket_intent: bool
     - response: str
     - issue: str (summary)
     - sentiment_score: float
     - department_id: int
     - category: str (upsell/request/complaint)
     - priority: str
     - outlet_id: int

   ParsedMenuRequest:
     - is_menu_request: bool
     - media: str
     - url: str
     - response: str
   ```

3. **Context Passed to LLM** (`parse_guest_message.py`):
   - Guest name, hotel name
   - Check-in/out dates
   - Room type
   - Guest preferences
   - Phase (Booking/Pre-check-in/During Stay/Post Checkout)
   - Departments info
   - Outlets info

4. **RAG Pipeline** (`rag_pipeline.py`):
   - Uses FAISS for vector search
   - Flattens hotel JSON into keypath:value pairs
   - Groups by top-level section
   - Chunks with RecursiveCharacterTextSplitter

**Files Extracted To**:
- `lumira-develop-extracted/lumira-develop/`
- `lumira-hotfix-extracted/lumira-hotfix-9-Jan-GJ-bot/`

**Next steps** (in order):
1. [x] Build Capability Registry ✅
2. [ ] Create Database Models (reference: common_models.py)
3. [ ] Build Response Validator
4. [ ] Implement Structured Data Service
5. [ ] Implement RAG Pipeline (reference: rag_pipeline.py)
6. [ ] Integrate Hybrid Router

---

### February 4, 2026 (Update 7)
**Status**: Capability Registry Implemented
**What was done**:
- Created comprehensive Capability Registry (`config/capabilities.py`)
- Integrated capability checking into chat service
- Updated LLM prompts to include actual hotel capabilities
- Added capability denial responses

**Files Created/Modified**:
```
new_bot/
├── config/
│   ├── __init__.py          # Updated exports
│   └── capabilities.py      # NEW - Full capability registry
├── services/
│   └── chat_service.py      # UPDATED - Capability checking
└── llm/
    └── client.py            # UPDATED - Capabilities in prompts
```

**Capability Registry Features**:

1. **Hotel Configuration**:
   - `HotelCapabilities` - Full hotel config (services, restaurants, transport)
   - `Restaurant` - Per-restaurant config (delivery zones, hours, reservations)
   - `TransportService` - Cab/transport config (local, airport, intercity)
   - `SpaService`, `RoomServiceConfig`, `HousekeepingConfig`

2. **Capability Checks**:
   - `room_delivery` - Can restaurant deliver to room?
   - `restaurant_reservation` - Table booking available?
   - `intercity_cab` - Usually NOT available!
   - `local_cab`, `airport_transfer`
   - `spa_booking`, `room_service`
   - `send_menu`, `send_multiple_menus`
   - `human_escalation`

3. **Default Test Hotel Config**:
   - IRD (In-Room Dining): Delivers to room
   - Kadak: Dine-in only (NO room delivery)
   - Aviator: Dine-in only (NO room delivery)
   - 24/7 Cafe: Delivers to room and pool
   - Transport: Local + Airport only (NO intercity!)

**Lumira Problems Fixed**:
| Problem | Fix |
|---------|-----|
| C: Promised Hyderabad cab | `intercity_cab` check returns false with helpful message |
| G: Single menu only | `can_send_multiple_menus` returns true |
| Kadak delivery | `room_delivery` check for Kadak returns false with alternatives |

**How It Works**:
```
User: "I want food from Kadak delivered to my room"
    ↓
Intent: order_food, entities: {restaurant: "Kadak"}
    ↓
Capability Check: room_delivery for Kadak
    ↓
Result: NOT ALLOWED - "Kadak is dine-in only"
    ↓
Response: "Kadak is dine-in only and does not offer room delivery.
           Alternatively, I can help you with: In-Room Dining, 24/7 Cafe"
```

**Test Cases**:
- "I want food from Kadak in my room" → Should deny with alternatives
- "Can you arrange a cab to Hyderabad?" → Should deny (intercity not available)
- "Show me all 3 menus" → Should work (multiple menus enabled)

**Next steps**:
1. [ ] Test capability checks in UI
2. [ ] Create Database Models
3. [ ] Build Response Validator
4. [ ] Implement Structured Data Service
5. [ ] **Admin Portal** (for managing capabilities without code changes)

---

### February 4, 2026 (Update 8)
**Status**: Fixed "Yes" Context + Admin Portal Requirement
**What was done**:
- Fixed "yes" confirmation not working when bot asks "Would you like to see the menu?"
- Added `show_menu` as a pending action type
- Documented Admin Portal requirement for managing capabilities

**Bug Fix**:
```
Before: Bot asks "Would you like to see the menu?" → User says "yes" → "I didn't understand"
After: Bot asks "Would you like to see the menu?" → User says "yes" → Shows menu ✅
```

**Admin Portal Requirement** (Future Feature):
| Feature | Purpose |
|---------|---------|
| Capability Manager | Enable/disable services per hotel |
| Restaurant Editor | Add/edit restaurants, delivery zones, hours |
| Menu Manager | Upload menus, set prices, availability |
| Prompt Editor | Customize bot responses |
| FAQ Manager | Add/edit FAQs for RAG |
| Analytics Dashboard | View conversations, escalation rates |

**Implementation Plan**:
1. **Phase 1**: Database models for capabilities (store in DB instead of code)
2. **Phase 2**: API endpoints for CRUD operations
3. **Phase 3**: React Admin Dashboard

---

### February 4, 2026 (Update 9)
**Status**: Admin Portal + Sample Data + Teammate Documentation Complete
**What was done**:
- Added 31 sample menu items across 4 restaurants
- Created Admin Portal (HTML + FastAPI, NOT React)
- Created comprehensive teammate task document

**Files Created/Modified**:
```
new_bot/
├── config/
│   └── capabilities.py      # UPDATED - Added 31 menu items
├── api/routes/
│   └── admin.py             # NEW - Admin API endpoints
└── templates/
    └── admin.html           # NEW - Admin Portal UI

Root folder:
└── TEAMMATE_TASKS.md        # NEW - Task assignment document
```

**Sample Menu Items Added**:
| Restaurant | Items |
|------------|-------|
| In-Room Dining (IRD) | 9 items (Butter Chicken, Biryani, Paneer Tikka, etc.) |
| Kadak | 8 items (Chai varieties, Samosa, Pakora, etc.) |
| Aviator Lounge | 8 items (Cocktails, mocktails, appetizers) |
| 24/7 Cafe | 6 items (Sandwiches, Pasta, Pizza, etc.) |

**Admin Portal Features**:
- **Capabilities Tab**: View hotel services, enable/disable
- **Restaurants Tab**: Toggle room delivery, reservations, status
- **Menus Tab**: View/add/delete menu items per restaurant
- **Transport Tab**: Configure cab services

**Admin API Endpoints**:
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/admin/api/hotels` | GET | List all hotels |
| `/admin/api/hotels/{code}` | GET | Get hotel details |
| `/admin/api/hotels/{code}/capabilities` | GET | Get capability summary |
| `/admin/api/hotels/{code}/restaurants` | GET | List restaurants |
| `/admin/api/hotels/{code}/restaurants/{id}/menu` | GET | Get menu items |
| `/admin/api/hotels/{code}/restaurants/{id}/menu` | POST | Add menu item |
| `/admin/api/hotels/{code}/restaurants/{id}/menu/{item}` | DELETE | Delete menu item |
| `/admin/api/hotels/{code}/restaurants/{id}` | PUT | Update restaurant settings |
| `/admin/api/hotels/{code}/transport` | PUT | Update transport settings |

**Teammate Task Document** (`TEAMMATE_TASKS.md`):
- Complete project context (what's done, structure)
- 6 detailed tasks with requirements:
  1. OCR Module for Menu Extraction (Medium)
  2. Database Models (Easy)
  3. Pydantic Schemas (Easy)
  4. Menu CRUD Service (Easy)
  5. Text Utility Functions (Easy)
  6. Unit Tests (Easy)
- Database table schemas (8 tables)
- Code examples and reference files
- Instructions for running the project

**Access Points**:
- Chat UI: http://localhost:8000/
- Admin Portal: http://localhost:8000/admin
- API Docs: http://localhost:8000/docs

**Next steps**:
1. [ ] Test Admin Portal functionality
2. [ ] Share TEAMMATE_TASKS.md with teammate
3. [ ] Implement database persistence (teammate + lead)
4. [ ] Build Response Validator
5. [ ] Implement RAG Pipeline

---

### February 5, 2026 (Update 10)
**Status**: Teammate's Code Integrated with MySQL Support
**What was done**:
- Discovered teammate's actual code was in files with "(1)" suffix (original files were empty)
- Integrated teammate's MySQL database code with hardcoded credentials
- Replaced placeholder code with teammate's actual implementations

**Teammate's Files Found** (in `teammate task codes/`):
```
Files with content (had "(1)" suffix):
- database (1).py      → 10,452 bytes (MySQL models + credentials)
- admin_schemas (1).py → 7,777 bytes (Pydantic schemas)
- menu_service (1).py  → 4,381 bytes (CRUD service)
- text_utils (1).py    → 2,463 bytes (Utility functions)
- conftest (1).py      → 625 bytes (Test fixtures)
- test_text_utils (1).py → 1,316 bytes
- test_menu_service (1).py → 3,003 bytes

Empty files (0 bytes):
- database.py, admin_schemas.py, menu_service.py, etc.
```

**MySQL Connection** (HARDCODED in database.py):
```python
HARDCODED_DATABASE_URL = "mysql+asyncmy://root:zapcom123@172.16.5.32:3306/GHN_PROD_BAK"
```
- Host: `172.16.5.32`
- Port: `3306`
- User: `root`
- Password: `zapcom123`
- Database: `GHN_PROD_BAK`
- **Requires OpenVPN to connect!**

**Table Names** (prefix `new_bot_`):
| Table | Purpose |
|-------|---------|
| `new_bot_hotels` | Hotel configuration |
| `new_bot_restaurants` | Restaurant within hotel |
| `new_bot_menu_items` | Menu items with prices |
| `new_bot_guests` | Hotel guest info |
| `new_bot_orders` | Food/service orders |
| `new_bot_order_items` | Individual items in order |
| `new_bot_conversations` | Chat session |
| `new_bot_messages` | Individual messages |

**UUID Storage**: BINARY(16) in MySQL (teammate's UUIDBinary class)

**Files Updated in Codebase**:
```
new_bot/
├── models/database.py       # Teammate's MySQL models
├── schemas/admin_schemas.py # Teammate's Pydantic schemas
├── services/menu_service.py # Teammate's CRUD service
├── utils/text_utils.py      # Teammate's utility functions
└── tests/
    ├── conftest.py          # Teammate's test fixtures
    ├── test_text_utils.py   # Teammate's tests
    └── test_menu_service.py # Teammate's tests
```

**To Test**:
1. Connect to OpenVPN
2. Run `pip install asyncmy` (MySQL async driver)
3. Run `python main.py` - Tables will auto-create
4. Run `pytest tests/ -v` (uses SQLite for tests)

**Teammate Task Status**:
| Task | Status |
|------|--------|
| Database Models | ✅ Done |
| Pydantic Schemas | ✅ Done |
| Menu CRUD Service | ✅ Done |
| Text Utilities | ✅ Done |
| Unit Tests | ✅ Done |
| OCR Module | ⏳ Pending (research ongoing) |

**Next steps**:
1. [ ] Connect OpenVPN and verify MySQL connection
2. [ ] Run server to create tables in GHN_PROD_BAK
3. [ ] Seed initial hotel/restaurant data
4. [ ] Build Response Validator
5. [ ] Implement RAG Pipeline
6. [ ] OCR Module (when research complete)

---

### February 5, 2026 (Update 11)
**Status**: Industry-Agnostic Config System Complete with Admin UI
**What was done**:
- Created industry-agnostic configuration system with JSON templates
- Added Business Config management to Admin Portal
- Implemented full JavaScript functionality for config management

**Industry-Agnostic Architecture**:
The bot is now fully config-driven, NOT code-driven:
- **No code changes needed** to adapt to different industries
- Configuration stored in `config/business_config.json`
- Templates available for quick setup: Hotel, Retail, Healthcare
- Admin UI allows real-time configuration changes

**Files Created**:
```
new_bot/
├── config/
│   ├── business_config.json              # Active configuration
│   └── templates/
│       ├── hotel_template.json           # Hotel industry template
│       ├── retail_template.json          # Retail industry template
│       └── healthcare_template.json      # Healthcare industry template
├── services/
│   └── config_service.py                 # Config management service
```

**Configuration Structure**:
```json
{
  "business": {
    "id": "business_id",
    "name": "Business Name",
    "type": "hotel|retail|healthcare",
    "city": "City",
    "bot_name": "Assistant",
    "welcome_message": "Hello! How can I help?"
  },
  "capabilities": {
    "food_ordering": {"enabled": true, "description": "..."},
    "room_service": {"enabled": true, "hours": "24/7"},
    "...": "..."
  },
  "services": [...],  // Restaurants, departments, etc.
  "intents": [...],   // Enabled intents
  "escalation": {
    "confidence_threshold": 0.4,
    "max_clarification_attempts": 3,
    "modes": ["live_chat", "ticket"]
  }
}
```

**Config API Endpoints** (in `admin.py`):
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/admin/api/config` | GET/PUT | Full config |
| `/admin/api/config/business` | GET/PUT | Business info |
| `/admin/api/config/capabilities/{id}` | PUT | Toggle capability |
| `/admin/api/config/services` | GET/POST/PUT/DELETE | Services CRUD |
| `/admin/api/config/intents/{id}` | PUT | Toggle intent |
| `/admin/api/config/escalation` | GET/PUT | Escalation settings |
| `/admin/api/config/templates` | GET | List templates |
| `/admin/api/config/templates/apply` | POST | Apply template |
| `/admin/api/config/export` | GET | Export as JSON |
| `/admin/api/config/import` | POST | Import from JSON |

**Admin Portal - Business Config Tab**:
- Business Information form (name, type, city, bot name, currency, welcome message)
- Industry Templates section (Hotel, Retail, Healthcare)
- Export/Import JSON config
- Capabilities toggle table
- Intents management (click to toggle)
- Escalation settings (confidence threshold, max clarifications, modes)

**JavaScript Functions Added** (in `admin.html`):
| Function | Purpose |
|----------|---------|
| `loadBusinessConfig()` | Load and display business info |
| `saveBusinessInfo()` | Save business info changes |
| `loadTemplates()` | List available templates |
| `applyTemplate()` | Apply a template |
| `exportConfig()` | Download config as JSON file |
| `importConfig()` | Upload config from JSON file |
| `loadCapabilitiesConfig()` | Load capabilities table |
| `toggleCapability()` | Enable/disable a capability |
| `loadIntentsConfig()` | Load intents as toggleable chips |
| `toggleIntent()` | Enable/disable an intent |
| `loadEscalationConfig()` | Load escalation settings |
| `saveEscalationConfig()` | Save escalation changes |

**How to Use for Different Industries**:
1. Go to Admin Portal (`/admin`)
2. Click "Business Config" tab
3. Either:
   - **Apply Template**: Click "Apply" on Hotel/Retail/Healthcare
   - **Import Config**: Upload existing JSON config
4. Customize capabilities, intents, escalation as needed
5. Bot automatically uses new configuration

**Industry Templates**:
| Template | Capabilities | Services |
|----------|--------------|----------|
| Hotel | Food ordering, room service, spa, transport, concierge | Restaurants, room service |
| Retail | Product search, order tracking, returns, support | Departments, stores |
| Healthcare | Appointments, doctor info, reports, prescriptions | Departments, clinics |

**Access Points**:
- Chat UI: http://localhost:8000/
- Admin Portal: http://localhost:8000/admin (Business Config is first tab)
- API Docs: http://localhost:8000/docs

**Next steps**:
1. [ ] Connect OpenVPN and verify MySQL connection
2. [ ] Run server to create tables in GHN_PROD_BAK
3. [ ] Seed initial hotel/restaurant data
4. [ ] Build Response Validator
5. [ ] Implement RAG Pipeline
6. [ ] OCR Module (when research complete)

---

### February 5, 2026 (Update 12)
**Status**: Enhanced Admin Portal for All Industries
**What was done**:
- Completely redesigned Admin Portal with Setup Wizard
- Added support for 12 industry types with pre-configured capabilities
- Created visual industry selector with icons
- Added capability suggestions per industry

**Industries Supported** (with pre-configured capabilities):
| Industry | Icon | Sample Capabilities |
|----------|------|---------------------|
| Hotel | 🏨 | Food ordering, room service, spa, transport, concierge |
| Restaurant | 🍽️ | Menu, ordering, table booking, delivery tracking |
| Spa & Wellness | 💆 | Treatment booking, therapist selection, packages |
| Hospital | 🏥 | Appointments, doctor info, reports, prescriptions |
| Automobile | 🚗 | Test drives, service booking, spare parts, quotes |
| Retail | 🛍️ | Product search, order tracking, returns, loyalty |
| Travel Agency | ✈️ | Flights, hotels, packages, visa, itinerary |
| Event Management | 🎉 | Venue booking, catering, decoration, vendors |
| Banquet Hall | 🎊 | Hall availability, capacity, menu packages |
| Education | 🎓 | Course info, admissions, fees, faculty |
| Real Estate | 🏠 | Property search, site visits, loans |
| Custom | ⚙️ | General inquiries, FAQ, feedback |

**New Admin Portal Tabs**:
1. **Setup Wizard** - Visual industry selector + business info form
2. **Capabilities** - Manage capabilities with add/delete + industry suggestions
3. **Services** - Add/manage departments, outlets, restaurants
4. **Intents** - Toggle which intents the bot recognizes
5. **Escalation** - Configure human handoff settings
6. **Advanced** - Export/import JSON, edit raw config

**Key Features**:
- **Visual Industry Selector**: Click industry card to select and get suggested capabilities
- **One-Click Template Apply**: Select industry → Click "Apply Industry Template"
- **Add Custom Capabilities**: Modal form to add any capability
- **Services Management**: Add departments/outlets specific to business
- **Capability Suggestions**: Shows relevant capabilities per industry, click to add
- **Export/Import**: Download/upload JSON config for backup or sharing
- **Raw JSON Editor**: For advanced users to edit config directly
- **Live Config Preview**: See current JSON in Advanced tab

**How to Configure for Any Industry**:
1. Go to `/admin`
2. **Step 1**: Click on your industry card (e.g., 🏥 Hospital)
3. **Step 2**: Fill in business name, city, bot name
4. **Step 3**: Click "Apply Industry Template"
5. **Customize**: Go to Capabilities tab to add/remove features
6. **Done**: Bot now works for that industry

**Access Points**:
- Admin Portal: http://localhost:8000/admin
- Chat Test: http://localhost:8000/
- API Docs: http://localhost:8000/docs

**Next steps**:
1. [ ] Connect OpenVPN and verify MySQL connection
2. [ ] Run server to create tables in GHN_PROD_BAK
3. [ ] Seed initial hotel/restaurant data
4. [ ] Build Response Validator
5. [ ] Implement RAG Pipeline
6. [ ] OCR Module (when research complete)

---

### February 5, 2026 (Update 13)
**Status**: Fixed Admin Portal → Chatbot Sync Issue
**What was done**:
- Fixed critical sync issue: Admin Portal changes now reflect in chatbot
- Updated `chat_service.py` to use `config_service` instead of `capability_registry`
- Added new methods to `config_service.py` for capability checking
- Ensured all Admin Portal fields (including welcome_message) are properly saved

**Root Cause Analysis**:
The chatbot was reading from `capability_registry` (in-memory, hardcoded) instead of `config_service` (JSON file). Admin Portal correctly saved to JSON but chatbot never read from it.

**Files Modified**:
```
new_bot/
├── services/
│   ├── chat_service.py        # UPDATED - Now uses config_service
│   └── config_service.py      # UPDATED - Added capability summary methods
```

**Changes to config_service.py**:
| Method | Purpose |
|--------|---------|
| `reload_config()` | Force reload from file (clear cache) |
| `get_capability_summary()` | Convert JSON config to chatbot format |
| `is_capability_enabled()` | Check if a specific capability is enabled |
| `get_welcome_message()` | Get welcome message with placeholders replaced |
| `get_service_by_id()` | Get service/restaurant by ID |
| `can_deliver_to_room()` | Check if service delivers to room |

**Changes to chat_service.py**:
- Replaced `from config.capabilities import capability_registry` with `from services.config_service import config_service`
- Updated `_check_capability_for_intent()` to use `config_service.is_capability_enabled()`
- Updated capability summary call to use `config_service.get_capability_summary()`

**Flow Before (BROKEN)**:
```
Admin Portal → config_service → JSON file (business_config.json)
                ↓
Chatbot → capability_registry → Hardcoded data (WRONG!)
```

**Flow After (FIXED)**:
```
Admin Portal → config_service → JSON file (business_config.json)
                ↓                        ↓
Chatbot → config_service ← ← ← ← ← ← ← ┘ (CORRECT!)
```

**What Now Syncs**:
| Field | Status |
|-------|--------|
| Business name | ✅ Synced |
| Welcome message | ✅ Synced |
| Bot name | ✅ Synced |
| City | ✅ Synced |
| Capabilities | ✅ Synced |
| Services | ✅ Synced |
| Intents | ✅ Synced |
| Escalation settings | ✅ Synced |

**Database Issue**:
Database tables exist but data isn't stored because:
1. DB requires OpenVPN connection to `172.16.5.32`
2. Current config uses JSON file persistence (works offline)
3. To use DB: Connect OpenVPN → Tables will auto-create on startup

**What Works Now Without OpenVPN**:
- ✅ Admin Portal configuration (JSON-based)
- ✅ Chatbot reads Admin Portal config
- ✅ All capability toggles
- ✅ Welcome message customization
- ✅ Business info updates

**What Requires OpenVPN**:
- ❌ Database persistence (MySQL)
- ❌ Menu item storage in DB
- ❌ Order history in DB
- ❌ Guest records in DB

**Next steps**:
1. [x] Admin Portal → Chatbot sync fixed ✅
2. [ ] Connect OpenVPN to enable MySQL
3. [ ] Seed initial data to database
4. [ ] Build Response Validator
5. [ ] Implement RAG Pipeline

---

### February 6, 2026 (Update 14)
**Status**: Database Models Updated from UUID to INT AUTO_INCREMENT
**What was done**:
- Updated all SQLAlchemy models in `database.py` to use `Integer` instead of `UUIDBinary` for IDs
- Removed UUID-related imports and `UUIDBinary` class
- Added relationships for `BusinessConfig`, `Capability`, `Intent` models to `Hotel`
- Index names updated to match `recreate_tables.py` (e.g., `idx_hotel`, `idx_rest`, `idx_conv`)

**Why Change to INT**:
- UUID as BINARY(16) displayed as garbled characters in database tools
- INT AUTO_INCREMENT is easier to debug and query
- Matches the table structure defined in `recreate_tables.py`

**Files Modified**:
```
new_bot/
└── models/database.py     # All models now use Integer IDs
```

**Model Changes Summary**:
| Model | Old ID Type | New ID Type |
|-------|-------------|-------------|
| Hotel | UUIDBinary | Integer |
| Restaurant | UUIDBinary | Integer |
| MenuItem | UUIDBinary | Integer |
| Guest | UUIDBinary | Integer |
| Order | UUIDBinary | Integer |
| OrderItem | UUIDBinary | Integer |
| Conversation | UUIDBinary | Integer |
| Message | UUIDBinary | Integer |
| BusinessConfig | UUIDBinary | Integer |
| Capability | UUIDBinary | Integer |
| Intent | UUIDBinary | Integer |

**New Relationships Added**:
- `Hotel.business_configs` → `BusinessConfig`
- `Hotel.capabilities` → `Capability`
- `Hotel.intents` → `Intent`
- Back-populates added to `BusinessConfig`, `Capability`, `Intent`

**Database Structure** (11 tables with INT AUTO_INCREMENT):
```
new_bot_hotels           → id INT AUTO_INCREMENT PRIMARY KEY
new_bot_business_config  → hotel_id INT FK → new_bot_hotels(id)
new_bot_capabilities     → hotel_id INT FK → new_bot_hotels(id)
new_bot_intents          → hotel_id INT FK → new_bot_hotels(id)
new_bot_restaurants      → hotel_id INT FK → new_bot_hotels(id)
new_bot_menu_items       → restaurant_id INT FK → new_bot_restaurants(id)
new_bot_guests           → hotel_id INT FK → new_bot_hotels(id)
new_bot_conversations    → hotel_id INT FK, guest_id INT FK (nullable)
new_bot_messages         → conversation_id INT FK → new_bot_conversations(id)
new_bot_orders           → guest_id INT FK, restaurant_id INT FK
new_bot_order_items      → order_id INT FK, menu_item_id INT FK
```

**Next steps**:
1. [x] Run `python recreate_tables.py` to recreate tables with INT IDs ✅
2. [x] Verify tables in MySQL have correct structure ✅
3. [x] Test application with new database structure ✅
4. [ ] Build Response Validator
5. [ ] Implement RAG Pipeline

---

### February 6, 2026 (Update 15)
**Status**: Database Seeded + UUID→INT Fix + End-to-End Working
**What was done**:
- Fixed `recreate_tables.py` connection drop issue (added `SET FOREIGN_KEY_CHECKS=0`, `ensure_closed()`)
- All 11 tables recreated successfully with INT AUTO_INCREMENT IDs
- Fixed UUID→INT mismatch across entire codebase:
  - `schemas/admin_schemas.py`: All `UUID` types changed to `int`
  - `services/db_config_service.py`: Removed `uuid` imports, changed types to `int`, removed `id=uuid.uuid4()` (let AUTO_INCREMENT handle it)
  - `services/menu_service.py`: Changed all `str` ID params to `int`
- Created `seed_data.py` script and seeded database with:
  - 1 hotel (Mangala Giri, Bangalore)
  - 4 restaurants (IRD, Kadak, Aviator Lounge, 24/7 Cafe)
  - 31 menu items across all restaurants
  - 9 business config entries (bot_name, welcome_message, escalation settings)
  - 10 capabilities (food_ordering, room_service, spa, transport, etc.)
  - 11 intents (greeting, order_food, menu_request, etc.)
- Tested end-to-end: Chat UI, Admin Portal, Health endpoint all working

**Files Created**:
- `seed_data.py` — Database seeding script

**Files Modified**:
- `recreate_tables.py` — Fixed FK checks + connection cleanup
- `schemas/admin_schemas.py` — UUID→int for all ID fields
- `services/db_config_service.py` — UUID→int, removed uuid imports
- `services/menu_service.py` — str→int for ID params

**Database State**:
| Table | Rows |
|-------|------|
| new_bot_hotels | 1 |
| new_bot_restaurants | 4 |
| new_bot_menu_items | 31 |
| new_bot_business_config | 9 |
| new_bot_capabilities | 10 |
| new_bot_intents | 11 |
| new_bot_guests | 0 |
| new_bot_conversations | 0 |
| new_bot_messages | 0 |
| new_bot_orders | 0 |
| new_bot_order_items | 0 |

**Next steps**:
1. [x] Fix Admin Portal → Chatbot config sync ✅
2. [ ] Build Response Validator
3. [ ] Implement RAG Pipeline
4. [ ] OCR Module (teammate research)

---

### February 6, 2026 (Update 16)
**Status**: Admin Portal → Chatbot Sync Fully Fixed
**What was done**:
- Fixed config_service.py cache issue — `load_config()` now always reads from JSON file instead of returning stale cached data
- Fixed LLM prompt — bot name was hardcoded as "Zack", now uses `bot_name` from admin config
- Fixed intent classifier — was using `hotel_code` instead of `hotel_name`
- Fixed greeting flow — now returns configured `welcome_message` from admin portal instead of LLM-generated greeting
- Removed stale `from uuid import UUID` import in admin.py

**Files Modified**:
- `services/config_service.py` — Removed cache-first logic in `load_config()`, always reads from file
- `llm/client.py` — Bot name from config instead of hardcoded "Zack"; hotel_name in classify_intent
- `services/chat_service.py` — Greeting uses `config_service.get_welcome_message()`
- `api/routes/admin.py` — Removed unused UUID import

**Root Cause**:
1. `config_service.load_config()` cached `_config` on first load, never re-read file after admin saves
2. `llm/client.py` had `named Zack` hardcoded in the system prompt
3. Greetings went through LLM generation instead of using configured welcome_message

**What Now Syncs**:
| Field | Admin Portal → Chatbot |
|-------|----------------------|
| Bot name | ✅ Used in LLM system prompt |
| Hotel name | ✅ Used in LLM context |
| Welcome message | ✅ Returned on greeting intent |
| Capabilities | ✅ Used for capability checks |
| Services/Restaurants | ✅ Used in LLM context |

**Next steps**:
1. [x] Handler Refactoring + Order Persistence (Phase 1) - DONE
2. [ ] Build Response Validator (Phase 2)
3. [ ] Context Persistence (Phase 3)
4. [ ] Implement RAG Pipeline (Phase 4)
5. [ ] Prompt Management (Phase 5)
6. [ ] Escalation Dashboard (Phase 6)
7. [ ] OCR Module (teammate research)

---

### February 6, 2026 (Update 17)
**Status**: Phase 1 Complete - Handler Registry + Order Persistence
**What was done**:
- Created modular handler architecture with BaseHandler, HandlerResult, and HandlerRegistry
- Created 9 intent handlers: Greeting, Menu, Order, Booking, Complaint, Escalation, Transport, RoomService, FAQ (stub)
- Refactored chat_service.py to use handler registry (dispatch to handlers first, LLM fallback for unhandled)
- Added DB session injection via FastAPI Depends(get_db) in chat route
- Order handler creates real Order + OrderItems records in DB with Guest lookup/upsert
- Menu handler fetches real menu items from DB with prices, categories, restaurant info
- Booking handler has full confirmation flow with booking reference generation
- Complaint handler captures complaints and offers manager escalation
- Transport handler detects intercity vs airport vs local, validates capability
- Room service handler detects request type (cleaning, towels, amenities, etc.)
- Escalation handler reads escalation config from admin portal
- Confirmation intents (yes/no) routed contextually to the handler that set pending_action

**Files Created** (12 new):
- `handlers/__init__.py` - Package init + handler registration
- `handlers/base_handler.py` - BaseHandler ABC + HandlerResult model + shared helpers
- `handlers/registry.py` - HandlerRegistry with dispatch()
- `handlers/greeting_handler.py` - Returns configured welcome message
- `handlers/menu_handler.py` - DB-backed menu with config fallback
- `handlers/order_handler.py` - Full order flow with DB persistence
- `handlers/booking_handler.py` - Table booking with confirmation
- `handlers/complaint_handler.py` - Complaint capture + escalation offer
- `handlers/escalation_handler.py` - Human escalation with config message
- `handlers/transport_handler.py` - Cab/airport/intercity detection
- `handlers/room_service_handler.py` - Housekeeping/amenity requests
- `handlers/faq_handler.py` - Stub (returns None, Phase 4 RAG)

**Files Modified** (2):
- `services/chat_service.py` - Uses handler_registry.dispatch(), _dispatch_to_handler() for contextual confirmations, LLM fallback preserved
- `api/routes/chat.py` - Added DB session dependency injection via Depends(get_db)

**Architecture**:
```
User Message -> Intent Classification (LLM)
             -> Capability Check
             -> Handler Registry Dispatch
                -> Handler returns HandlerResult (response, state, actions)
                -> OR returns None -> LLM Fallback
             -> Update Context + Return Response
```

**Next steps**:
1. [ ] Phase 2: Response Validator (validate LLM output against capabilities/DB)
2. [ ] Phase 3: Context Persistence (save conversations to DB)
3. [ ] Phase 4: RAG Pipeline for FAQ
4. [ ] Phase 5: Prompt Management (admin-editable prompts)
5. [ ] Phase 6: Escalation Dashboard

---

### February 6, 2026 (Update 18)
**Status**: Phase 1 Bugfixes - Menu & Order Handlers Fixed
**What was done**:
Testing revealed 2 broken flows (Menu + Order). Root causes found and fixed:

**Bug 1: Menu shows "no menus available"**
- Root cause: `menu_handler.py` checked `capabilities.get("hotel_id")` but `get_capability_summary()` never includes `hotel_id`
- Since db_session is always passed now (via Depends(get_db)), it always took the DB path, got `hotel_id=None`, returned empty
- Fix: Menu handler now queries `new_bot_hotels` table by hotel name to resolve hotel_id. Falls back to first active hotel if name doesn't match.

**Bug 2: Order always says "mention item names"**
- Root cause: LLM classifier prompt showed `"entities": {"item": "value"}` (singular string). Order handler expected `entities.get("items", [])` (plural list). LLM returned `{"item": "margherita pizza"}`, handler got empty list.
- Fix 1: Updated LLM prompt to explicitly request `"items": ["item1", "item2"]` as a list, with entity extraction rules
- Fix 2: Order handler now accepts both `items` (list) and `item` (string), and splits comma-separated strings

**Files Modified**:
- `llm/client.py` - Updated entity format in classify_intent prompt to use `items` as list + added ENTITY EXTRACTION RULES
- `handlers/menu_handler.py` - Added hotel_id lookup from DB by hotel name (+ fallback to first active hotel)
- `handlers/order_handler.py` - Handles both `item` (str) and `items` (list) entity formats from LLM

**Test Results (before fix)**:
| Feature | Status |
|---------|--------|
| Greeting | ✅ Working |
| Menu Request | ❌ "no menus available" |
| Order Food | ❌ "mention item names" |
| Table Booking | ✅ Working (full flow) |
| Complaint | ✅ Working (with escalation) |
| Transport (airport) | ✅ Working |
| Transport (intercity) | ✅ Correctly denied |
| Transport (local) | ✅ Working |
| Room Service | ✅ Asks room number |
| Capability Denial | ✅ Working |
| FAQ/LLM Fallback | ✅ Working |

---

### February 11, 2026 (Update 19)
**Status**: Phase 2 + Phase 3 Implemented (Initial), Phase 4 Bootstrapped
**What was done**:
- Implemented first Response Validator layer and integrated it into chat pipeline before final response return
- Added initial Hybrid Complexity Router (simple vs complex scoring + routing metadata)
- Implemented DB-backed context persistence flow in `context_manager` (conversation and messages persisted when DB session is available)
- Updated chat endpoints (`get session`, `list sessions`, `reset`, `delete`) to use DB-backed context retrieval
- Added confidence-aware handling in `chat_service`:
  - Low confidence triggers clarification
  - Repeated low confidence triggers escalation based on configured threshold/settings
- Started Phase 4 RAG implementation:
  - Added `services/rag_service.py` with retrieval + optional LLM answer synthesis
  - Added FAQ knowledge base seed file under `config/knowledge_base/hotel_faq.md`
  - Updated FAQ handler to use RAG first and fallback to LLM path when retrieval confidence is low

**Critical Bugfixes Completed**:
- Fixed capability key mismatch for food ordering:
  - `order_food` now checks either `food_ordering` OR `room_service`
- Fixed room number follow-up persistence:
  - Room number returned by room service handler metadata is now written back into conversation context
- Fixed SQLite test breakage due duplicate global index names:
  - Renamed SQLAlchemy index names to be globally unique
- Fixed test suite data-model mismatch:
  - Updated `tests/test_menu_service.py` to use int IDs (removed UUID + str conversions)
- Fixed datetime deprecation warning in `utils/text_utils.py`

**Files Created**:
- `core/complexity_router.py`
- `services/response_validator.py`
- `services/rag_service.py`
- `config/knowledge_base/hotel_faq.md`

**Files Modified**:
- `core/context_manager.py`
- `services/chat_service.py`
- `handlers/faq_handler.py`
- `api/routes/chat.py`
- `models/database.py`
- `tests/test_menu_service.py`
- `utils/text_utils.py`

**Validation**:
- Command run: `.\venv\Scripts\pytest.exe tests -q`
- Result: `12 passed`
- Command run: `.\venv\Scripts\python.exe -m compileall core services handlers api config utils schemas`
- Result: success

**Next steps**:
1. Implement full Agent Team orchestration for COMPLEX path (currently routed with metadata, not yet multi-agent execution)
2. Expand Response Validator with contradiction memory checks (assistant-vs-assistant consistency over turns)
3. Add document ingestion pipeline for RAG (chunking + embeddings + vector index)
4. Add admin-managed knowledge base upload and per-business RAG scoping
5. Add persistence for `pending_action`/`pending_data` in DB schema for full state recovery after restart

---

### February 11, 2026 (Update 20)
**Status**: Industry-Agnostic Controls Improved + Efficiency Pass
**What was done**:
- Added intent-enable enforcement in chat flow (admin intent toggles now directly gate workflows)
- Added intent alias mapping for cross-industry template compatibility
  - Example: core `table_booking` can map to healthcare `book_appointment`
- Added dynamic quick actions from enabled intents (less hotel-hardcoded behavior)
- Optimized config loading with mtime-based cache reload
  - Preserves admin-sync behavior while avoiding unnecessary file reads on every call
- Expanded capability checks for cross-industry capability keys
  - e.g. booking (`table_booking` OR `appointment_booking`), ordering (`food_ordering` OR `order_placement`)
- Improved LLM prompts to be business-type aware instead of hotel-only phrasing

**Error checks done**:
- Ran full tests: `.\venv\Scripts\pytest.exe -q` -> `12 passed`
- Added `.rgignore` to avoid recurring local tooling/search errors from `nul` and env/cache folders
- Verified no syntax/import issues after changes

**Files Modified**:
- `services/chat_service.py`
- `services/config_service.py`
- `llm/client.py`
- `.rgignore`

**Next steps**:
1. Add channel-aware quick actions (separate suggestions for Web widget vs WhatsApp)
2. Expand intent taxonomy beyond hotel-native `IntentType` to fully support retail/healthcare native intents
3. Add admin-managed intent-to-handler mapping so no code change is needed for new industries

---

### February 11, 2026 (Update 21)
**Status**: Admin Onboarding Flow Expanded (Industry-Agnostic Setup Steps Implemented)
**What was done**:
- Implemented end-to-end onboarding APIs aligned to the requested ARC-style flow:
  - Step 1: Extended business profile capture (location, address, timestamp format, language, contacts, website, channel flags)
  - Step 2: System prompt management + prompt template catalog + template application endpoint
  - Step 3: Knowledge base metadata + NLU do/don't rule management
  - Step 4: UI customization and channel-level settings (web widget/WhatsApp + theme fields)
- Added prompt-template pack for immediate industry use:
  - `generic_assistant`
  - `hotel_concierge`
  - `retail_sales_assistant`
  - `healthcare_frontdesk`
- Wired runtime LLM behavior to onboarding settings:
  - Intent classifier now consumes admin classifier prompt + NLU do/don't policies
  - Response generation now consumes admin system prompt + style + NLU do/don't policies
- Upgraded config schema handling:
  - Added backward-compatible defaulting/merge logic for new sections (`prompts`, `knowledge_base`, `ui_settings`, extended `business`)
  - Added mtime-safe schema upgrade persistence for old config files
- Expanded Admin Setup Wizard UI:
  - Added new cards for Prompt Behavior, Knowledge/NLU Rules, and UI/Channel customization
  - Added load/save/apply actions for all new onboarding sections
  - Extended existing business form with additional metadata fields
- Prevented data-loss on DB-backed full-config reads/writes:
  - `db_config_service.get_full_config()` now preserves JSON-only sections
  - `save_full_config()` now saves JSON first to retain onboarding-only sections
  - JSON reload behavior improved to avoid stale in-memory fallback reads

**Files Modified**:
- `api/routes/admin.py`
- `services/config_service.py`
- `services/db_config_service.py`
- `services/chat_service.py`
- `llm/client.py`
- `templates/admin.html`

**Files Added**:
- `config/prompt_templates/generic_assistant.json`
- `config/prompt_templates/hotel_concierge.json`
- `config/prompt_templates/retail_sales_assistant.json`
- `config/prompt_templates/healthcare_frontdesk.json`

**Validation**:
- Command run: `.\venv\Scripts\python.exe -m compileall api core services handlers llm models schemas utils`
- Result: success
- Command run: `.\venv\Scripts\pytest.exe -q`
- Result: `12 passed`

**Next steps**:
1. Map onboarding `industry_features` to concrete handler/plugin toggles (menu upload parser, catalog sync, appointment slots, etc.)
2. Add admin KB upload + parsing pipeline (PDF/CSV/URL ingestion) and connect to RAG indexing
3. Add channel-specific response formatting policies (WhatsApp-friendly short format vs web-widget rich format)
4. Add audit trail/versioning for onboarding changes (who changed prompt/policy and when)

---

### February 11, 2026 (Update 22)
**Status**: Database Migration Pack Added (Schema Efficiency + Tenant Consolidation)
**What was done**:
- Audited live MySQL schema and data distribution for all `new_bot_*` tables
- Identified split-tenant behavior risk (config in one hotel row, catalog in another) and prepared migration to consolidate safely
- Added a production-safe SQL migration pack with full runbook:
  - `00_precheck.sql` (conflict diagnostics, non-destructive)
  - `01_backup.sql` (snapshot backups to `bak_20260211_*`)
  - `02_up.sql` (schema upgrades + canonical hotel consolidation)
  - `03_verify.sql` (post-migration verification queries)
  - `04_down.sql` (rollback from snapshots + schema revert)
- Schema upgrades included in migration:
  - `new_bot_conversations.pending_action`
  - `new_bot_conversations.pending_data`
  - `new_bot_conversations.channel`
  - `new_bot_messages.channel`
  - `idx_messages_conv_created` on messages for ordered fetch performance
  - `idx_conversations_hotel_state` for state-filtered lookups
  - `idx_orders_rest_status_created` for operational order queries
- Data consolidation logic included:
  - merges business config/capabilities/intents to canonical hotel via upsert
  - reassigns restaurants/guests/conversations to canonical hotel
  - marks non-canonical hotels inactive
  - normalizes canonical code to `DEFAULT` for runtime lookup consistency

**Files Added**:
- `migrations/2026_02_11_db_efficiency/README.md`
- `migrations/2026_02_11_db_efficiency/00_precheck.sql`
- `migrations/2026_02_11_db_efficiency/01_backup.sql`
- `migrations/2026_02_11_db_efficiency/02_up.sql`
- `migrations/2026_02_11_db_efficiency/03_verify.sql`
- `migrations/2026_02_11_db_efficiency/04_down.sql`

**Notes**:
- Migration SQL was generated and reviewed; not auto-executed against production in this step
- Remaining application work: wire `pending_action` / `pending_data` / `channel` into DB read/write paths in context persistence

**Next steps**:
1. Run migration pack in order (`00 -> 01 -> 02 -> 03`) during a low-traffic window
2. Implement app-level persistence for new conversation columns
3. Add audit logging for admin config writes after DB consolidation

---

### February 11, 2026 (Update 23)
**Status**: DB Context Persistence Completed (Pending State + Channel)
**What was done**:
- Implemented end-to-end DB usage for migrated context fields:
  - Persist `pending_action` into `new_bot_conversations.pending_action`
  - Persist `pending_data` into `new_bot_conversations.pending_data`
  - Persist channel (`web` / `whatsapp`) into both `new_bot_conversations.channel` and `new_bot_messages.channel`
- Updated ORM mappings to match migrated schema:
  - `models/database.py`:
    - `Conversation.pending_action`
    - `Conversation.pending_data` (JSON)
    - `Conversation.channel`
    - `Message.channel`
  - Added ORM index declarations aligned with migration indexes:
    - `idx_messages_conv_created`
    - `idx_conversations_hotel_state`
- Updated context reconstruction logic:
  - `core/context_manager.py` now reloads `pending_action`, `pending_data`, and `channel` from DB instead of resetting them
  - Added channel normalization (`web_widget -> web`, `wa -> whatsapp`)
  - `update_state()` now supports clearing pending data by passing `{}` (previously empty dict could not clear)
- Updated chat request flow:
  - `schemas/chat.py`: added optional `channel` on request and `channel` in conversation context; default hotel code changed to `DEFAULT`
  - `services/chat_service.py`: passes request channel into context and stores channel metadata on messages
  - `api/routes/chat.py`: session endpoint now returns `channel`, `pending_data`, and per-message metadata
- Updated test UI defaults:
  - `static/js/chat.js`: default hotel code set to `DEFAULT`; sends `channel: web_widget`
  - `templates/chat.html`: added `DEFAULT` option as selected
- Improved datetime handling:
  - moved context/message defaults from deprecated `utcnow()` usage to timezone-aware UTC timestamps

**Files Modified**:
- `models/database.py`
- `core/context_manager.py`
- `schemas/chat.py`
- `services/chat_service.py`
- `api/routes/chat.py`
- `static/js/chat.js`
- `templates/chat.html`

**Files Added**:
- `tests/test_context_manager.py`

**Validation**:
- `.\venv\Scripts\python.exe -m compileall core schemas services api models static tests` -> success
- `.\venv\Scripts\pytest.exe -q` -> `14 passed`
- Live DB smoke check:
  - created context with channel + pending state
  - reloaded successfully with persisted values
  - cleanup (delete context) successful

**Next steps**:
1. Add channel-aware quick actions and response formatting rules (web vs whatsapp)
2. Add admin UI controls for channel defaults + per-channel policy prompts
3. Add audit fields (`updated_by`, `updated_source`) for conversation/config mutations

---

### February 11, 2026 (Update 24)
**Status**: Service/Intent Admin Controls Expanded + Runtime Context Hardened
**What was done**:
- Implemented full custom intent lifecycle (add/update/delete) with optional `maps_to` core-intent mapping:
  - API: `POST /admin/api/config/intents`, `PUT /admin/api/config/intents/{id}`, `DELETE /admin/api/config/intents/{id}`
  - JSON config + DB sync both supported (DB first, JSON fallback)
  - `maps_to` persisted via `new_bot_business_config` keys (`intent_map.<intent_id>`)
- Upgraded service persistence behavior to keep rich metadata:
  - Services now always save to JSON first (as source of truth for industry-agnostic fields like `type`, `description`, custom fields)
  - DB sync is applied for restaurant-compatible rows
  - Service reads now merge DB restaurant state with JSON metadata so descriptions/types are not lost
- Improved full-config DB save sync:
  - `save_full_config()` now reconciles services and intents (add/update/delete) instead of leaving them stale
- Expanded chatbot runtime context for LLM:
  - Added `service_catalog` and `intent_catalog` into LLM context
  - Added custom-intent resolution (`config_service.resolve_intent_to_core`) in classification path
  - Unknown custom intent can be mapped to a core runtime intent for handler compatibility
- Made response generation industry-agnostic:
  - Removed hotel-hardcoded capability rendering in LLM response prompt
  - Capabilities and services are now dynamically built from admin configuration
  - Service status/details (`active`, hours, zones, description) are included in prompt constraints
- Updated Admin UI:
  - Intents tab now supports add/delete plus mapping to core runtime intent
  - Services tab now includes edit action for name/description/type updates
  - Service list displays both type and ID for easier cross-reference with context/rules

**Files Modified**:
- `api/routes/admin.py`
- `services/config_service.py`
- `services/db_config_service.py`
- `services/chat_service.py`
- `llm/client.py`
- `templates/admin.html`

**Files Added**:
- `tests/test_config_service.py`

**Validation**:
- `python -m compileall api core services handlers llm models schemas tests` -> success
- `python -m pytest -q` -> `16 passed`

**Next steps**:
1. Replace prompt-based service editing in Admin UI with structured inline form fields (hours/zones/type-specific metadata)
2. Add admin guardrails so custom intents require `maps_to` (or an intent-to-handler plugin) before enabling
3. Add audit logs for service/intent mutations (`who`, `when`, `source`)
4. Add per-channel prompt policy blocks (web vs WhatsApp tone/length/format)

---

### February 12, 2026 (Update 25)
**Status**: RAG Pipeline Upgraded (Ingestion + Tenant Scoping + Optional Qdrant Backend)
**What was done**:
- Replaced bootstrap-only RAG implementation with production-oriented pipeline in `services/rag_service.py`:
  - Added document ingestion from knowledge files
  - Added chunking with overlap and metadata tags (`tenant_id`, `business_type`, `source`, `chunk_id`)
  - Added local persisted chunk index (`data/rag/local_index.json`) for deterministic fallback
  - Added tenant-scoped retrieval with optional fallback to `default` tenant
  - Added reranking stage (vector/score + lexical overlap blending)
  - Added optional Qdrant backend path with tenant payload filters (`tenant_id`) and graceful fallback to local retrieval
  - Added grounded answer generation that is industry-agnostic (uses `business_type`)
- Extended RAG agent and FAQ integration:
  - `agents/rag_agent.py` now accepts `tenant_id` + `business_type`
  - `handlers/faq_handler.py` now passes conversation tenant (`context.hotel_code`) and business type into RAG
- Added admin APIs for RAG operations:
  - `GET /admin/api/rag/status` (index/backend status)
  - `POST /admin/api/rag/reindex` (ingest/rebuild by tenant with optional file list)
  - `POST /admin/api/rag/query` (debug retrieval/answer endpoint)
- Added settings for controllable RAG behavior:
  - backend selection, chunk size/overlap, top_k, min retrieval score, rerank toggle
  - Qdrant connection + collection/vector settings
- Added CLI utility:
  - `run_rag_reindex.py` for manual reindex from terminal

**Files Modified**:
- `services/rag_service.py`
- `agents/rag_agent.py`
- `handlers/faq_handler.py`
- `api/routes/admin.py`
- `config/settings.py`
- `requirements.txt`

**Files Added**:
- `run_rag_reindex.py`
- `tests/test_rag_service.py`

**Validation**:
- `python -m compileall api agents handlers services config tests run_rag_reindex.py` -> success
- `python -m pytest -q` -> `19 passed`

**Next steps**:
1. Implement admin KB upload endpoint for files and background indexing jobs
2. Add support for URL/PDF/CSV extraction in ingestion stage
3. Add retrieval evaluation set + metrics dashboard (hit rate, groundedness, no-answer rate)
4. Add channel-aware RAG response formatting (short WhatsApp answers vs richer web answers)

---

### February 12, 2026 (Update 26)
**Status**: Complex Agent Orchestration Integrated into Chat Routing
**What was done**:
- Implemented a dedicated complex-path agent team orchestrator:
  - New module `agents/complex_query_orchestrator.py`
  - Flow:
    1. Try `rag_agent` for research/FAQ-style complex requests
    2. If retrieval is insufficient, invoke a complex response synthesis agent via LLM
- Wired orchestration into runtime chat routing:
  - `services/chat_service.py` now executes agent team for `ProcessingPath.COMPLEX` when no direct handler answer is available
  - Adds orchestration metadata in response (`agent_orchestration`, `agents_used`, rag diagnostics)
- Extended FAQ handler/RAG path context propagation:
  - tenant/business context already passed to RAG agent from prior step is now utilized in complex-team flow
- Reduced import-coupling risk:
  - made `services/__init__.py` side-effect free to avoid package-level circular import issues in agent wiring
- Updated backlog state:
  - `agents/RAG_AGENT_BACKLOG.md` now reflects completed ingestion/vector/rerank + complex orchestration

**Files Modified**:
- `services/chat_service.py`
- `agents/__init__.py`
- `agents/RAG_AGENT_BACKLOG.md`
- `services/__init__.py`

**Files Added**:
- `agents/complex_query_orchestrator.py`
- `tests/test_complex_query_orchestrator.py`

**Validation**:
- `python -m compileall agents services handlers tests` -> success
- `python -m pytest -q` -> `21 passed`

**Next steps**:
1. Add admin UI controls for RAG operations (status/reindex/query) instead of API-only access
2. Add file-upload + async background indexing jobs
3. Add agent policy layer for explicit escalation/guardrail enforcement on complex paths
4. Add per-channel routing policies (WhatsApp short format vs web richer format)

---

### February 12, 2026 (Update 27)
**Status**: Admin UI for RAG Controls Added (API-integrated)
**What was done**:
- Added a new **RAG & Agents** tab in admin portal (`templates/admin.html`)
- Added UI controls wired to existing RAG endpoints:
  - Status panel (`GET /admin/api/rag/status`)
  - Reindex action (`POST /admin/api/rag/reindex`)
  - Query test box (`POST /admin/api/rag/query`)
- Added tenant/business defaults synchronization from onboarding business config:
  - auto-uses business `id` as tenant
  - keeps business type aligned with selected industry
- Added realtime feedback in UI:
  - backend/qdrant/chunk badges
  - JSON status/reindex output panes
  - query result + confidence + sources
- Hooked RAG status refresh into general admin load flow and knowledge-save flow

**Files Modified**:
- `templates/admin.html`

**Validation**:
- `python -m compileall api agents handlers services config tests` -> success
- `python -m pytest -q` -> `21 passed`

**Next steps**:
1. Add file upload widget in RAG tab (instead of manual path entry)
2. Add async indexing jobs + job progress state
3. Add URL/PDF/CSV extraction pipeline and source parsing diagnostics

---

### February 12, 2026 (Update 28)
**Status**: RAG File Upload + Background Index Jobs Implemented
**What was done**:
- Added backend upload + async job orchestration for RAG:
  - `POST /admin/api/rag/upload` for multi-file knowledge uploads by tenant
  - `POST /admin/api/rag/jobs/start` to start background indexing
  - `GET /admin/api/rag/jobs` for recent job list
  - `GET /admin/api/rag/jobs/{job_id}` for per-job status
- Implemented new in-memory job manager service:
  - `services/rag_job_service.py`
  - job lifecycle: `queued -> running -> completed/failed`
  - stores report/error/timestamps for UI diagnostics
- Upgraded Admin RAG tab UI:
  - Upload files widget + optional auto-add-to-sources behavior
  - Async index job start button
  - Live current job badges + jobs list
  - Job polling until terminal state and status refresh on completion
- Kept existing synchronous reindex controls available for direct/manual use

**Files Modified**:
- `api/routes/admin.py`
- `templates/admin.html`

**Files Added**:
- `services/rag_job_service.py`
- `tests/test_rag_job_service.py`

**Validation**:
- `python -m compileall api services templates tests` -> success
- `python -m pytest -q` -> `23 passed`

**Next steps**:
1. Add persistent job storage (DB-backed) so status survives server restart
2. Add parsing/extraction workers for PDF/CSV/URL sources
3. Add source-level diagnostics (failed files, parse errors, skipped chunks)

---

### February 16, 2026 (Update 29)
**Status**: Stabilization + Production Readiness Backlog Defined
**What was done**:
- Performed end-to-end output review on admin + chat behavior with real hotel KB and custom rules.
- Verified system currently supports:
  - Admin onboarding + config management
  - Handler-based transactional flows
  - Conversation memory capture/summaries
  - RAG upload/reindex/query + async indexing jobs
- Verified latest automated quality snapshot:
  - `python -m pytest -q` -> `38 passed`
- Documented root-cause issues affecting production quality:
  1. Tenant/session identity mismatch across chat UI, admin tenant, and RAG retrieval scope
  2. Response validator false positives replacing valid booking confirmations
  3. Non-deterministic pending-action routing for short follow-up replies (`5`, `10 pm`, etc.)
  4. Reservation details not persisted/retrievable for "my booking" follow-up questions
  5. Weak follow-up context handling in short elliptical queries (`how much cost`)
  6. Escalation return path (`Return to bot`) does not reliably de-escalate state
  7. Menu duplication due to data-level duplicates + missing dedupe/constraints

**Current completion estimate**:
- Core platform: ~75%
- Demo readiness: ~80%
- Production readiness: ~55%

**Next steps (priority order)**:
1. Unify tenant/session identity across admin, chat session, and RAG scope
2. Fix response-validator false positives (especially inactive-service substring collisions)
3. Add deterministic pending-action routing before generic intent fallback
4. Persist reservation artifacts + add "my reservation" retrieval flow
5. Add memory-aware follow-up query rewriting for FAQ/RAG
6. Implement clean de-escalation handler for `Return to bot`
7. Add menu dedupe + DB uniqueness safeguards

---

### February 16, 2026 (Update 30)
**Status**: P0 Step 1 Completed - Tenant/Session Identity Normalization
**What was done**:
- Implemented canonical runtime hotel/tenant resolution in config service:
  - Added `resolve_hotel_code()` to map placeholder/default UI codes to configured `business.id`
  - Preserves explicit non-placeholder tenant IDs
- Wired chat runtime to use canonical tenant on every request:
  - `chat_service.process_message()` now resolves hotel code before context load/create
  - Existing sessions are normalized if they carry stale placeholder codes
- Added regression coverage:
  - Config-level tenant resolution test
  - Chat-service test verifying resolved tenant is used for context creation

**Files Modified**:
- `new_bot/services/config_service.py`
- `new_bot/services/chat_service.py`
- `new_bot/tests/test_config_service.py`
- `new_bot/tests/test_chat_service_faq_bank.py`
- `new_bot/progress.md`

**Validation**:
- `python -m pytest -q` -> `40 passed`

**Immediate next step**:
1. P0 Step 2: Fix response-validator false positives that overwrite valid confirmations

---

### February 16, 2026 (Update 31)
**Status**: P0 Core Runtime Stabilization Completed (Steps 2-7)
**What was done**:
- Hardened response validator inactive-service checks:
  - eliminated generic substring false positives (e.g., inactive `restaurant` id matching booking confirmation text)
  - now only flags when language implies availability/promise
- Made pending-action routing deterministic for short follow-ups:
  - active detail-collection actions bypass low-confidence clarification loops
  - pending-action-based handler routing expanded for booking/room-service flows
  - booking parser now accepts plain numeric party-size replies (e.g., `5`)
- Implemented persistent transaction memory capture:
  - reservation confirmations now persist booking reference/details in conversation memory facts
  - order confirmations now persist order details in memory facts
- Added deterministic personal-memory recall responses:
  - supports "my reservation time", "my order id/details", and known departure-time recall
- Added memory-aware follow-up query rewriting for retrieval:
  - short ambiguous follow-ups are contextualized with prior request + known facts
  - integrated in both FAQ handler and complex-query orchestrator RAG calls
- Fixed escalation return flow:
  - `Return to bot` now cleanly de-escalates `ESCALATED` state back to normal bot interaction
- Added menu dedup + uniqueness enforcement:
  - runtime menu rendering deduplicates duplicate item rows
  - menu service now upserts by normalized `(restaurant_id, name, category)` identity and blocks conflicting updates

**Files Modified**:
- `new_bot/services/response_validator.py`
- `new_bot/services/chat_service.py`
- `new_bot/services/conversation_memory_service.py`
- `new_bot/handlers/booking_handler.py`
- `new_bot/handlers/order_handler.py`
- `new_bot/handlers/faq_handler.py`
- `new_bot/agents/complex_query_orchestrator.py`
- `new_bot/handlers/menu_handler.py`
- `new_bot/services/menu_service.py`
- `new_bot/tests/test_conversation_memory_service.py`
- `new_bot/tests/test_chat_service_faq_bank.py`
- `new_bot/tests/test_policy_and_service_runtime.py`
- `new_bot/tests/test_menu_service.py`
- `new_bot/progress.md`

**Validation**:
- `python -m pytest -q` -> `50 passed`

**Immediate next step**:
1. P1: health-support workflow + admin conflict warnings + channel parity hardening

---

### February 17, 2026 (Update 32)
**Status**: Runtime Quality Hardening for Demo Consistency
**What was done**:
- Implemented pending-flow interruption logic so slot-filling does not hijack unrelated user queries.
- Scoped DO-rule validation by intent to avoid over-aggressive replacement prompts during table booking.
- Added booking-time operating-hours validation before confirmation to block invalid reservations.
- Added deterministic menu recommendation path for preference asks (e.g., non-veg/spicy) to reduce unnecessary escalation.
- Improved order/menu consistency:
  - order lookup now respects active restaurant status
  - typo-tolerant menu item matching added
  - item-option menu requests return filtered results instead of full unrelated menu
- Added runtime observability marker (`pending_interrupted`) for intent/routing clarity.

**Files Modified**:
- `new_bot/services/chat_service.py`
- `new_bot/services/response_validator.py`
- `new_bot/handlers/booking_handler.py`
- `new_bot/handlers/order_handler.py`
- `new_bot/handlers/menu_handler.py`
- `new_bot/tests/test_manual_issue_fixes.py`
- `new_bot/tests/test_policy_and_service_runtime.py`
- `new_bot/tests/test_menu_order_consistency.py`
- `new_bot/tests/test_chat_service_recommendation.py`
- `new_bot/progress.md`

**Validation**:
- `python -m pytest -q` -> `58 passed`

**Immediate next step**:
1. Implement industry-agnostic health-support workflow + admin conflict warnings

---

*Last Updated: February 17, 2026*

---

### February 17, 2026 (Update 33)
**Status**: Transcript Error Fix Pass (Routing + Booking + Order + DB Fallback)

**What was done**:
- Added DB-fallback handling in chat API route:
  - if DB session errors (`OperationalError`), request is retried with in-memory mode instead of returning immediate generic failure
  - response metadata now includes `db_fallback=true` on fallback path
- Added deterministic service-overview shortcut in chat runtime:
  - messages like `what services are availabke` now return configured/active services instead of going to RAG no-match
- Added deterministic room-number profile update shortcut:
  - messages like `my room number is 202` now update context and return explicit acknowledgment
- Improved booking flow when service catalog is sparse:
  - service name hint extraction from booking text (`book table at kadak`)
  - free-form service acceptance when no service catalog is configured
  - recovery of missing service from previous user turn during `select_service` step
  - fallback service labels from enabled capability flags when catalog rows are empty
- Prevented validator from overriding active booking slot-filling prompts:
  - DO-rule replacement is skipped for booking pending-actions (`select_service`, `collect_booking_party_size`, `collect_booking_time`, etc.)
- Improved order handling for weak/noisy item requests:
  - category keyword fallback (`snacks`, `drink`, `dessert`, etc.) now returns available options
  - typo/no-match fallback now proposes closest item suggestions
- Improved service-shortcut routing boundaries:
  - service-catalog action shortcut no longer intercepts clear restaurant booking transactions (routes back to booking handler)
- Added detailed transcript RCA document:
  - `CHATBOT_TRANSCRIPT_ROOT_CAUSE_ANALYSIS_2026-02-17.md`

**Files Modified**:
- `new_bot/api/routes/chat.py`
- `new_bot/services/chat_service.py`
- `new_bot/handlers/booking_handler.py`
- `new_bot/handlers/order_handler.py`
- `new_bot/services/response_validator.py`
- `new_bot/CHATBOT_TRANSCRIPT_ROOT_CAUSE_ANALYSIS_2026-02-17.md`
- `new_bot/PROGRESSs.md`

**Validation**:
- Syntax check:
  - `python -m py_compile api/routes/chat.py services/chat_service.py handlers/booking_handler.py handlers/order_handler.py services/response_validator.py` -> passed
- Runtime smoke check:
  - chat startup + `POST /api/chat/message` greeting flow -> returned `200` with handler response
- Test run:
  - `python -m pytest -q` -> `90 passed, 6 failed`
  - Remaining failures are pre-existing alignment gaps (menu recommendation helper missing, legacy `restaurants` expectation in config summary, and a few policy-string expectation mismatches)

**Immediate next step**:
1. Implement remaining pre-existing test-gap fixes (`_match_menu_recommendation_response`, `restaurants` compatibility in config summary, and policy wording alignment) to bring suite to full green.

### February 17, 2026 (Update 34)
**Status**: Transcript Alignment + Remaining Test Gaps Closed (All Green)

**What was done**:
- Fixed deterministic recommendation filtering for preference asks:
  - corrected non-veg vs veg detection (`nonveg` no longer matches veg branch by substring)
  - recommendation responses now honor dietary preference filters consistently
- Added alias-aware menu intent classification for bare restaurant phrases:
  - messages like `in room dining`, `ird`, and `ird menu` now route to `MENU_REQUEST`
  - classified entities now include the matched restaurant name for downstream handling
- Improved order capability checks for explicit outlet delivery restrictions:
  - capability guard now extracts outlet mentions from user text (for example `from kadak`)
  - blocked-delivery reason now explicitly includes `dine-in only`
- Updated menu capability wording in RAG-first mode:
  - when menu runtime is disabled, capability reason now explicitly states `knowledge-base` lookup path
- Hardened confirmation fallback path to prevent zero-item confirmations:
  - `confirm_order` with no pending items now asks user to provide items instead of producing fake/empty order confirmations
  - LLM fallback state transition now stays in `AWAITING_INFO` for empty-item confirmation attempts
- Restored legacy `restaurants` compatibility in config capability summary:
  - derived `restaurants` list from normalized `service_catalog`
  - includes compatibility fields (`cuisine` fallback from description, `delivers_to_room`, `dine_in_only`, `hours`, `is_active`)

**Files Modified**:
- `new_bot/services/chat_service.py`
- `new_bot/services/config_service.py`
- `new_bot/PROGRESSs.md`

**Validation**:
- Targeted regression checks:
  - `python -m pytest -q tests/test_chat_service_recommendation.py tests/test_config_service.py::test_service_description_flows_into_capability_summary tests/test_config_service.py::test_capability_summary_handles_legacy_none_service_fields tests/test_policy_and_service_runtime.py::test_classify_intent_prefers_menu_for_restaurant_alias_phrase tests/test_policy_and_service_runtime.py::test_order_capability_check_blocks_explicit_non_delivery_restaurant tests/test_policy_and_service_runtime.py::test_menu_capability_check_allows_rag_mode_when_menu_runtime_disabled` -> `6 passed`
- Full suite:
  - `python -m pytest -q` -> `96 passed, 0 failed`
- Syntax check:
  - `python -m py_compile services/chat_service.py services/config_service.py handlers/booking_handler.py handlers/order_handler.py services/conversation_memory_service.py` -> passed

**Immediate next step**:
1. Re-run your long manual transcript scenario end-to-end in UI/API to verify the behavioral fixes under real conversation memory continuity.

### February 17, 2026 (Update 35)
**Status**: Transcript Crash Fix + Intent Guardrail Tightening (DB-outage safe)

**What was done**:
- Fixed order confirmation crash on malformed pending items:
  - `handlers/order_handler.py` now normalizes `pending_data["items"]` across string/list/dict shapes before processing
  - removed unsafe assumptions that every item is a dict with `.get()`
  - confirmation now works in fallback mode even when pending items came from LLM/string context
  - fallback confirmation no longer hard-fails; uses `Total: To be confirmed` when price data is unavailable
- Hardened service-overview shortcut to avoid false positives:
  - `do you provide medical services` no longer triggers generic "currently enabled services" listing
  - shortcut now requires broad overview phrasing/markers for list-all responses
- Expanded room-stay detection language:
  - phrases like `im looking for a room for 2` now correctly map to room-stay booking limitation handling
  - prevents accidental table-booking slot prompts for room-stay requests
- Improved API fallback metadata safety:
  - `api/routes/chat.py` now safely initializes `response.metadata` dict before writing `db_fallback=true`

**Files Modified**:
- `new_bot/handlers/order_handler.py`
- `new_bot/services/chat_service.py`
- `new_bot/api/routes/chat.py`
- `new_bot/tests/test_menu_order_consistency.py`
- `new_bot/tests/test_policy_and_service_runtime.py`
- `new_bot/PROGRESSs.md`

**Validation**:
- Targeted regressions:
  - `python -m pytest -q tests/test_menu_order_consistency.py::test_order_handler_confirmation_handles_string_pending_items_without_crash tests/test_policy_and_service_runtime.py::test_table_booking_capability_check_blocks_room_stay_lookup_language tests/test_policy_and_service_runtime.py::test_service_overview_shortcut_skips_specific_medical_service_query` -> `3 passed`
- Full suite:
  - `python -m pytest -q` -> `99 passed, 0 failed`
- Syntax check:
  - `python -m py_compile handlers/order_handler.py services/chat_service.py api/routes/chat.py` -> passed

**Immediate next step**:
1. Re-run the exact manual transcript with DB down to confirm no confirmation-path crashes and no generic service-list response for medical-service queries.

### February 17, 2026 (Update 36)
**Status**: RAG Retrieval Quality Upgrade (Query Rewrite + Larger Candidate Pool + MMR)

**What was done**:
- Implemented query normalization + variant rewrite inside `RAGService` retrieval path:
  - normalizes common typo/variant forms (`whatis`, `checkin/check-in`, `checkout/check-out`, `timings`, etc.)
  - builds bounded variant set for robust retrieval while preserving existing user-facing behavior
- Upgraded candidate retrieval strategy:
  - retrieval now overfetches an expanded candidate pool before final selection
  - pool size is dynamically bounded to the requested `20-40` range via config-backed knobs
- Added MMR-based diverse chunk selection:
  - integrated Maximal Marginal Relevance selection with configurable `lambda` (default `0.7`)
  - reduces repetitive chunk selection while preserving relevance
- Added optional LLM rerank stage (disabled by default):
  - can reorder MMR-selected chunks for answer-bearing priority when enabled
  - safe fallback keeps deterministic order on failures
- Extended runtime settings for safe tuning (backward-compatible defaults):
  - `rag_enable_mmr`, `rag_mmr_lambda`, `rag_candidate_pool_min`, `rag_candidate_pool_max`, `rag_enable_llm_rerank`
- Updated environment template with full RAG settings block for deployment parity.

**Files Modified**:
- `new_bot/services/rag_service.py`
- `new_bot/config/settings.py`
- `new_bot/.env.example`
- `new_bot/tests/test_rag_service.py`
- `new_bot/PROGRESSs.md`

**Validation**:
- Targeted RAG tests:
  - `python -m pytest -q tests/test_rag_service.py` -> `10 passed`
- Full suite:
  - `python -m pytest -q` -> `102 passed, 0 failed`
- Syntax check:
  - `python -m py_compile services/rag_service.py handlers/faq_handler.py agents/rag_agent.py agents/complex_query_orchestrator.py config/settings.py` -> passed

**Immediate next step**:
1. Run your fixed manual transcript set and compare before/after metrics: false "not found", duplicate context, and groundedness.

### February 17, 2026 (Update 37)
**Status**: LLM Query-Repair Before RAG Retrieval Enabled

**What was done**:
- Added explicit LLM query rewrite stage before RAG retrieval to correct typos/misspellings:
  - new guarded method in `RAGService` rewrites user query with strict constraints (no intent/entity/date invention)
  - rewrite is validated for semantic overlap; unsafe rewrites are discarded and original query is used
- Updated answer pipeline to use rewritten+normalized query for retrieval while preserving original user question for final answer generation prompt.
- Added runtime toggles (default enabled for rewrite):
  - `rag_enable_llm_query_rewrite`
  - `rag_llm_query_rewrite_max_tokens`
- Extended status payload to expose rewrite enablement flag.
- Updated `.env.example` with new RAG rewrite settings.

**Files Modified**:
- `new_bot/services/rag_service.py`
- `new_bot/config/settings.py`
- `new_bot/.env.example`
- `new_bot/tests/test_rag_service.py`
- `new_bot/PROGRESSs.md`

**Validation**:
- Targeted tests:
  - `python -m pytest -q tests/test_rag_service.py` -> `11 passed`
- Full suite:
  - `python -m pytest -q` -> `103 passed, 0 failed`
- Syntax check:
  - `python -m py_compile services/rag_service.py config/settings.py tests/test_rag_service.py` -> passed

**Immediate next step**:
1. Run a manual typo-heavy FAQ batch (e.g., `whatis chec in timng`, `im hungfry room food`) and compare retrieval hit quality before/after.

### February 18, 2026 (Update 38)
**Status**: Detailed Per-Response RAG Step Logs Added

**What was done**:
- Implemented detailed step-trace logging for the full RAG answer pipeline into a persistent file:
  - default log file: `./logs/detailedsteps.log`
  - one structured trace per RAG answer with:
    - `trace_id`, tenant/business context, timestamps
    - per-step status (`success` / `failed` / `skipped` / `no_change`)
    - input/output payload snippets for each stage
- Logged stages include:
  - input received
  - LLM query rewrite
  - normalization + query profiling
  - candidate retrieval per query variant (backend + counts + top chunk previews)
  - dedupe, MMR, lexical rerank, optional LLM rerank
  - final retrieval output, score gate, context build
  - grounded answer generation outcome
- Added trace correlation to runtime metadata:
  - `RAGAnswer` now carries `trace_id`
  - `RAGAgentResult` propagates `trace_id`
  - FAQ and complex orchestrator metadata now include `rag_trace_id`
- Added new settings/env controls:
  - `RAG_STEP_LOGS_ENABLED`
  - `RAG_STEP_LOG_FILE`
  - `RAG_STEP_LOG_PREVIEW_CHARS`

**Files Modified**:
- `new_bot/services/rag_service.py`
- `new_bot/agents/rag_agent.py`
- `new_bot/handlers/faq_handler.py`
- `new_bot/agents/complex_query_orchestrator.py`
- `new_bot/config/settings.py`
- `new_bot/.env.example`
- `new_bot/tests/test_rag_service.py`
- `new_bot/PROGRESSs.md`

**Validation**:
- Targeted tests:
  - `python -m pytest -q tests/test_rag_service.py tests/test_rag_job_service.py` -> `14 passed`
- Full suite:
  - `python -m pytest -q` -> `104 passed, 0 failed`
- Syntax check:
  - `python -m py_compile services/rag_service.py agents/rag_agent.py handlers/faq_handler.py agents/complex_query_orchestrator.py config/settings.py tests/test_rag_service.py` -> passed

**Immediate next step**:
1. Inspect `logs/detailedsteps.log` after live chats and correlate each response with `rag_trace_id` in metadata.

### February 18, 2026 (Update 39)
**Status**: Human-Readable RAG Step Logs Simplified

**What was done**:
- Updated `detailedsteps.log` format to include a simple 5-step readable block before raw JSON trace:
  1. User query
  2. Rewritten query
  3. RAG selected chunks
  4. Input sent to LLM
  5. LLM output
- Kept full JSON trace below summary for deep debugging compatibility.
- Added explicit LLM input fields in trace (`question_for_llm`, `context_preview`) so step 4 is always visible.
- Extended regression test to verify the new readable step headings exist in log output.

**Files Modified**:
- `new_bot/services/rag_service.py`
- `new_bot/tests/test_rag_service.py`
- `new_bot/PROGRESSs.md`

**Validation**:
- `python -m pytest -q tests/test_rag_service.py` -> `12 passed`
- `python -m pytest -q` -> `104 passed, 0 failed`
- `python -m py_compile services/rag_service.py tests/test_rag_service.py` -> passed

**Immediate next step**:
1. Open `logs/detailedsteps.log` after next FAQ/menu query and review the new 1->5 summary block per trace.

### February 18, 2026 (Update 40)
**Status**: Added Separate KB Direct Lookup Path (Without Removing RAG)

**What was done**:
- Implemented a new deterministic KB lookup service:
  - `services/kb_direct_lookup_service.py`
  - Reads structured KB sources (including wrapped `{"data":"{\"editable\":...}"}` format).
  - Normalizes noisy user text (including stripping `| Context: ...` follow-up suffixes).
  - Uses heuristic answers for high-value FAQ cases:
    - floors
    - address
    - terminal proximity / T1 vs T2
    - largest room
    - Prestige Suite bathtub
    - WiFi availability
    - smart laundry closet
    - bar/alcohol availability
  - Falls back to generic fact match (token overlap + threshold) when no heuristic fires.
- Added detailed trace logging for KB direct lookup into `logs/detailedsteps.log`:
  - Human-readable block + structured JSON trace
  - Includes query, normalized query, matched field, output preview, and status.
- Integrated KB direct lookup into FAQ flow as a separate stage before RAG:
  - `handlers/faq_handler.py`
  - If KB direct lookup handles the query, response is returned immediately.
  - If not handled, existing RAG flow runs exactly as before.
  - Added metadata fields for observability:
    - `kb_direct_lookup_used`
    - `kb_direct_lookup_attempted`
    - `kb_direct_lookup_confidence`
    - `kb_direct_lookup_reason`
    - `kb_direct_lookup_trace_id`
    - `kb_direct_lookup_field`
    - `kb_direct_lookup_source`
- Added new config/env controls:
  - `KB_DIRECT_LOOKUP_ENABLED`
  - `KB_DIRECT_LOOKUP_MIN_SCORE`
  - `KB_DIRECT_LOOKUP_MAX_ANSWER_CHARS`
  - `KB_DIRECT_LOOKUP_STEP_LOGS_ENABLED`

**Files Modified**:
- `new_bot/services/kb_direct_lookup_service.py` (new)
- `new_bot/handlers/faq_handler.py`
- `new_bot/config/settings.py`
- `new_bot/.env.example`
- `new_bot/tests/test_kb_direct_lookup_service.py` (new)
- `new_bot/tests/test_faq_handler.py`
- `new_bot/PROGRESSs.md`

**Validation**:
- Targeted tests:
  - `./venv/Scripts/python.exe -m pytest -q tests/test_faq_handler.py tests/test_kb_direct_lookup_service.py` -> `5 passed`
- Full suite:
  - `./venv/Scripts/python.exe -m pytest -q` -> `107 passed, 0 failed`
- Syntax check:
  - `python -m py_compile services/kb_direct_lookup_service.py handlers/faq_handler.py config/settings.py tests/test_kb_direct_lookup_service.py tests/test_faq_handler.py` -> passed

**Immediate next step**:
1. Run your known failing chat cases (for example `is wifi free`, `is alcohol available`, `largest room`) and compare `kb_direct_lookup_*` metadata + `KB DIRECT LOOKUP TRACE` blocks in `logs/detailedsteps.log`.

### February 18, 2026 (Update 41)
**Status**: KB Lookup LLM Rewrite Enabled + RAG Fallback Disabled (KB-Only Test Mode)

**What was done**:
- Enabled LLM-assisted rewrite in KB direct lookup path (before deterministic fact matching):
  - Added async KB lookup entrypoint: `answer_question_async(...)`
  - Added `rewrite_query_llm` tracing step in KB direct trace
  - Added safety validation so rewrite cannot drift far from original intent
- Extended KB normalization for common typo patterns:
  - `adress -> address`
  - `avaalble/avaiable -> available`
  - `ameticies -> amenities`
  - `flovors -> flavors`
- Switched FAQ handler to call the async KB lookup path.
- Added KB-only mode behavior:
  - If KB lookup does not find a confident answer, do **not** fallback to RAG
  - Return: “not found in current knowledge base” message directly
- Added config flags:
  - `KB_DIRECT_LOOKUP_ENABLE_LLM_REWRITE`
  - `KB_DIRECT_LOOKUP_LLM_REWRITE_MAX_TOKENS`
  - `KB_DIRECT_DISABLE_RAG_FALLBACK`

**Files Modified**:
- `new_bot/services/kb_direct_lookup_service.py`
- `new_bot/handlers/faq_handler.py`
- `new_bot/config/settings.py`
- `new_bot/.env.example`
- `new_bot/tests/test_faq_handler.py`
- `new_bot/PROGRESSs.md`

**Validation**:
- Targeted tests:
  - `./venv/Scripts/python.exe -m pytest -q tests/test_faq_handler.py tests/test_kb_direct_lookup_service.py` -> `6 passed`
- Full suite:
  - `./venv/Scripts/python.exe -m pytest -q` -> `108 passed, 0 failed`
- Syntax check:
  - `python -m py_compile services/kb_direct_lookup_service.py handlers/faq_handler.py config/settings.py tests/test_faq_handler.py tests/test_kb_direct_lookup_service.py` -> passed

**Immediate next step**:
1. Restart server and run UI chat to verify KB-only behavior in `logs/detailedsteps.log`:
   - expect `KB DIRECT LOOKUP TRACE` for every FAQ turn
   - expect no `RAG FLOW TRACE` for KB-miss turns while KB-only mode is enabled.

---

## Update: Full KB LLM Mode (No Retrieval Chunks)

**What was done**:
- Added a separate full-KB runtime mode that sends complete KB text to LLM each turn and bypasses RAG chunk retrieval.
- Wired chat pipeline to use this mode first when enabled.
- Added structured multi-turn state handling via LLM JSON output:
  - intent
  - next state
  - pending action
  - pending data
  - normalized query
- Added detailed per-turn logs in `logs/detailedsteps.log`:
  - `FULL KB LLM FLOW TRACE`
  - includes user query, normalized query, KB sources/chars, LLM decision, final response
- Added environment/config toggles for easy switching between full-KB and legacy modes.

**Files Modified**:
- `new_bot/services/full_kb_llm_service.py`
- `new_bot/services/chat_service.py`
- `new_bot/config/settings.py`
- `new_bot/.env.example`
- `new_bot/progress.md`
- `new_bot/PROGRESSs.md`

**Validation**:
- Syntax check: passed
  - `.\venv\Scripts\python.exe -m compileall config/settings.py services/full_kb_llm_service.py services/chat_service.py`
- Runtime smoke check: passed
  - response path shows `response_source=full_kb_llm`
  - log file contains `FULL KB LLM FLOW TRACE` blocks
  - state transitions persisted across turns

---

## Update: Full-KB Food Flow Quality Fixes

**Problem observed**:
- In full-KB mode, food conversations were getting stuck and repeating narrow responses (for example only two pizza options).
- Short follow-ups like `in room` were not advancing slot collection.

**What was done**:
- Added deterministic food-flow guardrails inside `services/full_kb_llm_service.py`:
  - parse complete menu catalog from escaped KB format
  - expand pizza matching across all pizza/flatbread items from full KB
  - capture order slots from terse messages (`item`, `quantity`, `room_number`, `delivery_location`)
  - handle typo-intent mapping (`margherita` variants -> nearest in-menu equivalent)
  - enforce summary -> confirmation -> final confirmed response progression
- Added new detailed trace steps for observability:
  - `parse_menu_catalog`
  - `food_guardrails` with reason codes

**Validation**:
- `.\venv\Scripts\python.exe -m compileall services/full_kb_llm_service.py`
- Manual smoke flow passed:
  - `i want pizza` -> full pizza options list
  - `in room` -> in-room slot captured
  - `i want margethrirta` -> mapped to `ICONIQA NEAPOLITAN FLATBREAD`
  - `1 pizza 202` -> order summary with total
  - `yes` -> confirmed order response
- Logs confirm fix behavior with:
  - `pizza_items_count: 6`
  - `food_in_room_slot_captured`
  - `food_ready_for_confirmation`
  - `food_confirmation_yes`

---

## Update: Removed Hardcoded Food Rules (Pure LLM Full-KB Mode)

**Reason**:
- Shift from short-term deterministic patches to long-term LLM-driven behavior quality.

**What was done**:
- Replaced `services/full_kb_llm_service.py` with a pure LLM-first implementation.
- Removed hardcoded food-specific runtime logic:
  - keyword lists
  - quantity/room extraction helpers
  - typo alias mapping rules
  - deterministic post-LLM food guardrail overrides
- Preserved full-KB architecture and logging:
  - user query
  - normalized query
  - full KB sources/chars
  - LLM decision output

**Validation**:
- `.\venv\Scripts\python.exe -m compileall services/full_kb_llm_service.py services/chat_service.py config/settings.py`
- Smoke check passed:
  - `response_source=full_kb_llm`
  - `full_kb_llm_mode=True`
## 2026-02-18 20:35:37 - Context + Confirmation + Full-KB Flow Stabilization

- Strengthened full-KB memory usage:
  - Added forced summary refresh path for full-KB mode (`full_kb_llm_force_summary_refresh`).
  - Increased configurable memory summary budget (`full_kb_llm_memory_summary_chars`).
  - Passed `memory_recent_changes` to full-KB LLM input each turn.
- Improved conversation memory extraction:
  - Added stay-date parsing for compact and natural ranges (e.g., `feb 20-23`, `from 20 feb to 23 feb`).
  - Captures `stay_checkin_date`, `stay_checkout_date`, `stay_date_range`, and booking day hints.
- Fixed full-KB pending-context loss:
  - Pending data is now merged as updates instead of being dropped when LLM omits fields.
  - Added support for LLM-driven `clear_pending_data` flag for explicit reset behavior.
- Added strict confirmation gate:
  - New settings: `chat_require_strict_confirmation_phrase`, `chat_confirmation_phrase` (default `yes confirm`).
  - Confirmation is accepted only for exact phrase in strict mode.
  - If phrase is wrong, bot asks for exact confirmation text and keeps pending flow active.
- Improved prompt policies (generic, non-hardcoded):
  - Explicitly separates KB facts vs user/session memory facts.
  - Adds rule to route modification/change requests to staff (no fabricated direct confirmations).
  - Instructs proactive option suggestion when user intent is broad.
  - Requires 3-5 context-aware suggested actions for UI chips.
- Added context-aware suggested-action fallback builder:
  - Uses state + pending action + active service catalog for dynamic quick actions.
  - Replaces static-only fallback in full-KB path when model does not return chips.

### Updated files
- `config/settings.py`
- `services/conversation_memory_service.py`
- `services/full_kb_llm_service.py`
- `services/chat_service.py`

### Verification
- `python -m compileall services config schemas` passed.
## 2026-02-18 20:45:16 - Strict Confirmation Scope Fix

- Root cause fixed: strict confirmation guard was triggering on any non-empty pending_action, including non-final steps like room browsing (oom_booking).
- Updated full-KB guard logic to enforce yes confirm only when confirmation is actually required:
  - pending action starts with confirm_, or
  - state is waiting_confirmation, or
  - last assistant message is an explicit confirmation prompt.
- Added generic confirmation-prompt detector (_is_confirmation_prompt_text) and strict-check helper (_requires_strict_confirmation).
- Updated full-KB system prompt wording to clarify strict phrase applies to final confirmation steps only.
- Compile verification passed: python -m compileall services.
## 2026-02-18 20:54:09 - Room Booking Context + Confirmation Flow Hardening

- Fixed premature room-booking confirmation in full-KB flow:
  - Added guard that blocks final confirmation prompts until required stay details are present.
  - Required details checked from pending data + memory facts: guest count, check-in, check-out (or date range).
  - Missing details now trigger deterministic slot-collection prompt and collect_room_booking_details pending action.
- Fixed yes/no polarity drift in selection phase:
  - Added lightweight binary reply correction for selection flows.
  - Prevents cases where user yes was interpreted as confirmation_no and looped.
  - In room selection flow, stale oom_type is reset when user confirms they want to continue exploring.
- Added helper methods in chat service for:
  - room-flow detection,
  - missing-field detection,
  - deterministic missing-details prompt generation.
- Updated full-KB prompt policy:
  - Explicitly requires collecting room booking essentials before final confirmation.
- Compile verification passed: python -m compileall services.
## 2026-02-18 21:16:33 - Room Memory Alias Fix + Yes-Selection Rewrite

- Fixed room-booking missing-field guard alias gap:
  - Guard now recognizes check_in / check_out (in addition to checkin_date / checkout_date).
  - This prevents false re-prompts for guest/date details when those values already exist in pending data or memory.
- Added affirmative-selection rewrite:
  - In waiting_selection, bare yes now maps to the first option from the previous assistant would you like to X or Y? prompt.
  - Reduces yes -> unclear loops and improves continuity without hardcoding room/menu-specific options.
- Added metadata flag ffirmative_selection_rewrite_applied for observability in responses.
- Compile verification passed: python -m compileall services.
## 2026-02-18 21:43:11 - Bubble UX + Room Availability Branch Progression

- Implemented bubble post-processing to keep suggestions user-askable:
  - Filters bot-instruction style suggestions (Provide/Specify/Enter/Type ...).
  - Preserves contextual chips and falls back to domain-aware askable prompts.
  - Enforces concise deduped suggestion set (max 4).
- Added room availability branch progression guard:
  - If user explicitly asks availability and model repeats the same choice question, bot now advances flow.
  - Generates a concrete forwarding confirmation prompt with known booking details and yes confirm action.
  - Sets pending action to confirm_room_availability_check.
- Prompt strengthened for better conversational progression:
  - Instructed LLM not to repeat the same branch-choice question after user selection.
  - Instructed that suggested_actions must be user-askable, not bot instructions.
- Compile verification passed: python -m compileall services.
## 2026-02-18 21:52:32 - Generalized Explicit Confirmation Enforcement

- Added generalized final-confirmation normalization in full-KB flow:
  - Whenever 
ext_state=awaiting_confirmation, pending action is normalized to a confirm_* action.
  - If strict confirmation mode is enabled, bot response is normalized to include explicit instruction to type the configured confirmation phrase.
- This prevents cases where bot asks generic Would you like to proceed? without explicit yes confirm guidance.
- Added helpers:
  - _contains_explicit_confirmation_instruction
  - _ensure_explicit_confirmation_instruction
  - _normalize_confirm_pending_action
- Compile verification passed: python -m compileall services.
## 2026-02-18 22:49:37 - Generalized Order Pre-Confirmation Workflow

- Added deterministic, generalized pre-confirm checklist for order_food in full-KB flow.
- Prevents premature order confirmation by enforcing staged collection:
  1) item selection,
  2) quantity (and optional group size),
  3) add-more/drink step,
  4) final confirmation (yes confirm).
- Added order-context slot extraction independent of LLM JSON reliability:
  - extracts item, quantity, guest hints, and add-on choices from compact user replies,
  - keeps order slots stable across turns via pending-data merge.
- Added generic handling for add-ons step:
  - yes asks for what to add,
  - 
o moves to final review prompt.
- Added deterministic review prompt before final confirmation for consistency.
- Added observability metadata: deterministic_order_updates.
- Compile verification passed: python -m compileall services.
## 2026-02-18 23:35:00 - Exhaustive Options Listing (KB + Recommendation Path)

- Full-KB LLM prompt strengthened to prevent partial/sampled option replies.
- Added explicit instruction to return complete matching options for list/show/recommend/show-more queries.
- Added explicit rule to include all matching category items (e.g., burger/pizza/red wine/room types) with prices when available.
- Updated response style policy: concise for normal Q&A, exhaustive for option-list requests.
- Updated deterministic menu recommendation helper in `services/chat_service.py`:
  - broadened trigger detection for recommendation/listing asks,
  - added focus-term extraction (including follow-up "show more options" context reuse),
  - removed top-5 truncation,
  - now returns all matching available options (deduplicated) instead of a sample subset.
- Compile verification passed: `python -m compileall services`.
## 2026-02-18 23:48:00 - Follow-up "More Options" Context Fix

- Added generic follow-up rewrite for option-list continuation in full-KB mode:
  - Detects messages like "show more", "do u hv more", "any more", "more options".
  - Carries forward topic from current/previous user turn.
  - Rewrites to explicit instruction: return only additional unseen options, or clearly say no more options.
- Integrated rewrite into full-KB turn pipeline before LLM call.
- Added observability metadata:
  - `more_options_rewrite_applied`
  - uses shared rewrite detection with `affirmative_selection_rewrite_applied`.
- Strengthened full-KB prompt policy:
  - follow-up "more" must avoid repeating already listed items,
  - must return only additional options or explicit "no additional options".
- Compile verification passed: `python -m compileall services`.
## 2026-02-19 00:03:00 - Order Category Follow-up Fix (Which Burger)

- Fixed order-flow issue where category-level order requests (e.g., "order a burger") were immediately pushed to quantity collection, causing follow-ups like "which burger" to loop on quantity prompt.
- In full-KB order flow (`collect_order_quantity`), added a generic follow-up detector for option-list asks (which/what/show/list/options/available/more).
- When such follow-up is detected:
  - switches pending action to `collect_order_item`,
  - clears stale quantity slot,
  - returns a concrete options list response instead of repeating quantity prompt.
- Added generic category-hint derivation and dynamic menu lookup from DB to list all matching options with prices.
- Improved item extraction normalization by stripping leading articles (`a/an/the`) from extracted order item hints.
- Compile verification passed: `python -m compileall services`.
## 2026-02-19 00:19:00 - Full Category Options Fix for Awaiting-Selection Order Flow

- Fixed case where `Show burger options` in `awaiting_selection` still relied on partial LLM listing.
- Added generic order-flow override for option-list follow-ups when pending action is `order_food` / `collect_order_item` / `collect_order_quantity`.
- The override now builds a full category options response and keeps flow on `collect_order_item` (instead of drifting to quantity).
- Enhanced `_build_order_options_list_response`:
  - primary source: runtime menu DB (all matches),
  - secondary augmentation: full-KB extraction via LLM when DB coverage is low,
  - deduplicates and returns complete option list with prices when available.
- Added structured parser `_extract_option_entries_from_llm_json` for robust option extraction.
- Compile verification passed: `python -m compileall services`.

## 2026-02-23 15:10:00 - Medication / Health-Support Workflow (Architecture Increment)

- Added explicit core intent: `health_support`.
- Added new handler: `handlers/health_support_handler.py`:
  - emergency keyword detection with immediate safety-first escalation message
  - non-emergency safe policy response (no diagnosis/dosage) with confirmation gate
  - explicit handoff path via `confirm_health_support` pending action
- Updated runtime routing in `services/chat_service.py`:
  - pending-action ownership map now includes `confirm_health_support -> health_support`
  - deterministic health-support intent detection for medication/medical-help requests
  - support for confirmation replies in health-support pending flow
  - intent enablement aliases include `health_support`, `medical_support`, `medical_help`
  - specific medical-service info queries are excluded from generic service-overview shortcut
- Updated full-KB mode mappings in `services/full_kb_llm_service.py`:
  - parses `health_support` / `medical_support` intents
  - includes `health_support` in allowed intent list for structured outputs
- Updated LLM classifier prompt in `llm/client.py`:
  - `health_support` added to available intents and extraction guidance
- Added safety validator guardrail in `services/response_validator.py`:
  - blocks dosage/diagnosis-style responses for medical requests
  - replaces with safe handoff response
- Tests added: `tests/test_health_support_workflow.py`.

### Validation
- `python -m compileall handlers/health_support_handler.py services/response_validator.py services/chat_service.py` passed.
- `python -m pytest -q tests/test_health_support_workflow.py` -> `5 passed`.
- `python -m pytest -q tests/test_policy_and_service_runtime.py::test_service_overview_shortcut_skips_specific_medical_service_query` -> `1 passed`.
- `python -m pytest -q tests/test_manual_issue_fixes.py tests/test_policy_and_service_runtime.py` -> `38 passed`.

## 2026-02-23 16:20:00 - Service-Aware FAQ Blending + KB Conflict Warnings + Runtime Guardrail Parity

- Implemented service-aware FAQ/runtime blending in `handlers/faq_handler.py`:
  - FAQ responses from KB direct lookup and RAG are now post-processed with runtime `service_catalog` context.
  - Inactive service availability claims are deterministically overridden.
  - Dine-in-only vs room-delivery contradictions are deterministically overridden.
  - Added blend observability metadata: `service_context_blended`, `service_context_reason`, `service_context_service_id`.

- Added onboarding validation for setup-vs-KB conflicts in `services/config_service.py`:
  - New scanner inspects configured knowledge sources against current services.
  - Detects:
    - `inactive_service_marked_available`
    - `active_service_marked_unavailable`
    - `dine_in_only_conflicts_with_kb_delivery`
    - `room_delivery_conflicts_with_kb_dine_in_only`
  - Returns evidence snippets and source file references.
  - Exposed new admin endpoint in `api/routes/admin.py`:
    - `GET /admin/api/config/onboarding/knowledge/conflicts`

- Closed safety parity gap across runtime modes in `services/chat_service.py`:
  - Response validator is now explicitly applied in `chat_kb_only_mode` flow.
  - Response validator is now explicitly applied in `chat_full_kb_llm_mode` flow (passthrough and non-passthrough branches).
  - Added mode-level metadata:
    - `response_validator_applied`
    - `response_validator_valid`
    - `response_validator_replaced`
    - `response_validator_issues`

- Stabilized default runtime mode configuration in `config/settings.py`:
  - `chat_full_kb_llm_mode = False`
  - `chat_kb_only_mode = False`
  - `full_kb_llm_passthrough_mode = False`

### Validation
- `python -m compileall services/config_service.py handlers/faq_handler.py services/chat_service.py api/routes/admin.py tests/test_faq_handler.py tests/test_config_service.py tests/test_chat_service_faq_bank.py` passed.
- `python -m pytest -q` -> `118 passed`.

## 2026-02-23 19:10:00 - Architecture Completion Pass (excluding channel parity + ticketing E2E + OCR integration)

- Implemented API gateway hardening in runtime:
  - Added request middleware controls with trace IDs (`X-Trace-Id`), response-time headers, API-key auth checks, and rate-limiting guardrails for protected chat/admin API paths.
  - Added in-memory gateway diagnostics snapshot (`services/gateway_service.py`) for ops visibility.

- Implemented production observability plumbing:
  - Added structured JSONL event logging service (`services/observability_service.py`).
  - Added log status + recent event tail APIs:
    - `GET /admin/api/observability/status`
    - `GET /admin/api/observability/events`

- Implemented evaluation dashboard backend:
  - Added in-memory evaluation metrics collector (`services/evaluation_metrics_service.py`).
  - Added evaluation APIs:
    - `GET /admin/api/evaluation/summary`
    - `GET /admin/api/evaluation/events`
  - Added alert derivation in summary for spikes:
    - validator replacement rate
    - low-confidence rate
    - complex-path rate

- Integrated telemetry into chat API path:
  - `api/routes/chat.py` now records evaluation metrics per response (config-gated).
  - Added trace ID propagation into chat response metadata.
  - Added observability events for normal processing, DB fallback path, and failures.

- Extended admin dashboard UI (`templates/admin.html`):
  - Added new **Evaluation** tab with:
    - summary cards
    - evaluation summary JSON panel
    - recent evaluation events panel
    - observability runtime status panel
    - recent observability events panel with filter

- Upgraded complex-path architecture implementation (`agents/complex_query_orchestrator.py`):
  - Added explicit staged agent-team flow:
    - orchestrator planning step
    - research step (RAG)
    - planner step
    - executor step (safe inline actions only)
    - response synthesizer step
  - Preserved backward-compatible metadata keys (`rag_*`, `agents_used`, `complex_response_agent`) while adding richer `agent_team` metadata.

- Added release operations docs:
  - `docs/06_RELEASE_CHECKLIST.md`
  - `docs/07_ROLLBACK_PLAYBOOK.md`

- Updated architecture status docs:
  - `docs/04_ARCHITECTURE.md` now reflects implemented status + remaining deferred integrations.

### Validation
- `python -m compileall services api agents core handlers config schemas main.py` passed.
- `python -m pytest -q` -> `121 passed`.

## 2026-02-23 20:45:00 - Runtime Audit + Ops Notes (No Logic Changes)

- Cleared runtime logs to reset diagnostics baseline:
  - `logs/detailedsteps.log`
  - `logs/observability.log`
- Audited active chat KB/RAG runtime path and confirmed current `.env` behavior:
  - `CHAT_FULL_KB_LLM_MODE=true`
  - `FULL_KB_LLM_PASSTHROUGH_MODE=true`
  - `CHAT_KB_ONLY_MODE=false`
- In this configuration, normal chat flow enters full-KB LLM mode and bypasses chunk-based RAG retrieval during message handling.
- Confirmed RAG codepaths/endpoints are still wired and available in code:
  - `handlers/faq_handler.py` (FAQ retrieval path)
  - `agents/complex_query_orchestrator.py` (complex research path)
  - `api/routes/admin.py` (`/api/rag/*` endpoints)
- Clarified impact for potential RAG removal:
  - Deleting RAG code directly will break current imports/routes.
  - Removing RAG with full refactor can keep current full-KB mode running, but removes retrieval/indexing and fallback capability.
- Prepared fresh-push handoff steps for new remote:
  - `https://github.com/shaaista/kebov2`

## 2026-02-24 22:10:00 - Lumira Ticketing Integration (Read-Only DB + Local Safe Writes + Full-KB Routing)

- Completed Lumira ticketing discovery and contract documentation for Engage + Guest Journey:
  - Added table usage report with required/conditional tables and rationale:
    - `engage_guest_journey_db_tables_report.md`
  - Added integration blueprint with API/table/flow mapping:
    - `lumira_ticketing_integration_blueprint.md`
  - Added locked dev contract snapshot from provided DB evidence:
    - `lumira_ticketing_dev_contract_locked.md`
  - Added execution/testing runbook:
    - `ticketing_integration_test_guide.md`

- Implemented Lumira-style read adapters (DB reads only):
  - New repository:
    - `integrations/lumira_ticketing_repository.py`
  - Supports:
    - department lookup by entity
    - outlet lookup by entity
    - candidate open/in-progress ticket lookup
    - RI/FMS mapping lookup
    - agent lookup for handoff path

- Implemented/extended ticketing service contract and normalization:
  - `services/ticketing_service.py`
  - Normalization aligned to observed dev data:
    - `phase`: `Booking | Pre Checkin | During Stay | Post Checkout | Pre Booking`
    - `priority`: `CRITICAL | HIGH | MEDIUM | LOW`
    - category normalization + synonyms
    - `ticket_status` normalization
    - source normalization: `whatsapp_bot | booking_bot | manual | taskly`
  - Added payload compatibility fields:
    - `group_id`, `message_id`, token/cost fields, aliases (`department_id`, `category`, `sub_category`)

- Added safe local ticket mode (to avoid external DB/API writes while integrating):
  - `TICKETING_LOCAL_MODE` + `TICKETING_LOCAL_STORE_FILE`
  - Create/update ticket writes persist into:
    - `data/ticketing/local_tickets.json`
  - Local mode bypasses external base URL requirement and still keeps ticketing enabled.
  - `.env.example` and `config/settings.py` updated for these toggles.

- Added smart ticket routing service:
  - `services/ticketing_router_service.py`
  - Supports create vs update vs acknowledge decisions (LLM-first + heuristic fallback).

- Connected ticketing flows into handlers:
  - `handlers/complaint_handler.py`:
    - create flow
    - update-note flow
    - status flow
    - candidate routing + enrichment
  - `handlers/escalation_handler.py`:
    - optional ticket creation + handoff path
  - Handoff remains safely disabled when `AGENT_HANDOFF_API_URL` is empty.

- Updated DB engine selection to prefer configured environment URL:
  - `models/database.py` now uses `settings.database_url` first, then legacy fallback.

- Full-KB mode integration completed (no RAG required):
  - `services/chat_service.py` now supports hybrid full-KB routing:
    - full-KB LLM still generates responses/intent/state
    - ticket-related turns can route into complaint/escalation handlers
  - Added robust routing for operational cases where model returns non-complaint labels.
  - Added explicit LLM ticketing signal support:
    - `requires_ticket` (bool)
    - `ticket_reason` (string)
  - Full-KB prompt contract updated in:
    - `services/full_kb_llm_service.py`
  - Ticket route metadata now includes:
    - `full_kb_ticketing_handler`
    - `full_kb_ticketing_requested`
    - `full_kb_ticketing_reason`

- Runtime safe-mode status (current intent):
  - Full-KB mode ON, no RAG dependency for chat path.
  - Ticket create/update persisted locally (no external write side effects).
  - Legacy table usage remains read-only for enrichment/routing.

### Validation
- Targeted ticketing + full-KB routing tests passed:
  - `tests/test_ticketing_service_payload.py`
  - `tests/test_ticketing_router_service.py`
  - `tests/test_complaint_ticket_enrichment.py`
  - `tests/test_ticketing_local_mode.py`
  - `tests/test_chat_service_faq_bank.py` (ticketing/full-KB routing cases)
- Latest targeted run: `15 passed`.

## 2026-02-24 23:05:00 - Full-KB Ticketing Decision Hardening + Confirmation Flow Fixes

- Verified full-KB request context wiring in `services/full_kb_llm_service.py` includes:
  - last N messages (`recent_history`, default 10 from settings),
  - pending action/task state (`pending_action`, `pending_data`),
  - conversation memory summary + facts (`memory_summary`, `memory_facts`, `memory_recent_changes`),
  - dynamic capabilities/runtime state (`capabilities_summary`),
  - admin configuration (`admin_config`),
  - current user text.

- Hardened ticketing route control in `services/chat_service.py`:
  - Added explicit extraction of LLM ticketing signals from full-KB output:
    - `requires_ticket`
    - `ticket_reason`
  - Routing now respects explicit model intent:
    - `requires_ticket=true` => route into ticketing handler.
    - `requires_ticket=false` => do not auto-route (except active ticket confirm/update pending flows).
  - Prevented accidental ticket hijack for service flows:
    - `order_food` and `table_booking` no longer auto-route to complaint/ticket creation unless explicit ticket intent or pending ticket state exists.
  - Added route metadata for observability:
    - `full_kb_ticketing_requested`
    - `full_kb_ticketing_reason`

- Improved pending ticket confirmation UX in `handlers/complaint_handler.py`:
  - While waiting on yes/no for ticket creation, if user sends a new issue/request, flow now switches to that new issue instead of forcing yes/no only.

- Updated full-KB prompt contract in `services/full_kb_llm_service.py`:
  - Model is now explicitly instructed to always return:
    - `requires_ticket` (boolean)
    - `ticket_reason` (short string)
  - Guidance added: set `requires_ticket=true` only when staff action is actually needed.

- Added/updated regression tests:
  - `tests/test_chat_service_faq_bank.py`
    - `test_full_kb_order_without_requires_ticket_keeps_order_flow`
    - `test_full_kb_requires_ticket_true_routes_order_to_complaint_handler`
    - `test_full_kb_requires_ticket_false_skips_ticketing_route`
  - `tests/test_complaint_ticket_enrichment.py`
    - `test_confirm_ticket_creation_allows_switch_to_new_issue`

### Validation
- Ran targeted suites:
  - `tests/test_chat_service_faq_bank.py::test_full_kb_order_without_requires_ticket_keeps_order_flow`
  - `tests/test_chat_service_faq_bank.py::test_full_kb_requires_ticket_true_routes_order_to_complaint_handler`
  - `tests/test_chat_service_faq_bank.py::test_full_kb_requires_ticket_false_skips_ticketing_route`
  - `tests/test_complaint_ticket_enrichment.py::test_confirm_ticket_creation_allows_switch_to_new_issue`
  - `tests/test_ticketing_local_mode.py`
  - `tests/test_ticketing_service_payload.py`
  - `tests/test_ticketing_router_service.py`
- Result: `12 passed`.

## 2026-02-24 23:35:00 - Booking Flow Guard Against Ticketing Hijack

- Fixed full-KB ticket routing behavior for booking detail collection:
  - Updated `services/chat_service.py` `_should_route_full_kb_ticketing_handler`.
  - `table_booking` flow now remains conversational and does not auto-switch to complaint/ticket flow only because `requires_ticket=true`.
  - Ticket routing for booking now requires one of:
    - active ticketing pending action, or
    - explicit ticketing/operational issue markers in user text.

- Added regression coverage:
  - `tests/test_chat_service_faq_bank.py`
    - `test_full_kb_table_booking_with_requires_ticket_keeps_booking_flow`

### Validation
- `python -m pytest -q tests/test_chat_service_faq_bank.py::test_full_kb_table_booking_with_requires_ticket_keeps_booking_flow tests/test_chat_service_faq_bank.py::test_full_kb_requires_ticket_true_routes_order_to_complaint_handler tests/test_chat_service_faq_bank.py::test_full_kb_requires_ticket_false_skips_ticketing_route tests/test_chat_service_faq_bank.py::test_full_kb_order_without_requires_ticket_keeps_order_flow`
- Result: `4 passed`.

## 2026-02-24 23:50:00 - Order Confirmation Slot-Data Compatibility Fix

- Fixed `yes confirm` order failure where bot replied:
  - `"I don't have the order items yet..."`
- Root cause:
  - `handlers/order_handler.py` confirm path expected only `pending_data.items`,
  - full-LLM flow stores slot fields like `order_item`, `order_quantity`, `order_total`.

- Implemented generalized fallback reconstruction in `handlers/order_handler.py`:
  - `_rebuild_items_from_slot_pending(...)` builds normalized items from slot keys.
  - Confirmation now supports both legacy `items[]` and slot-based pending data.
  - Uses DB lookup (when available) for menu item/restaurant enrichment; otherwise confirms safely without DB.

- Added helper:
  - `_first_non_empty(...)` for robust field fallback extraction.

- Added regression test:
  - `tests/test_menu_order_consistency.py`
    - `test_order_handler_confirmation_rebuilds_slot_based_pending_items`

### Validation
- `python -m pytest -q tests/test_menu_order_consistency.py::test_order_handler_confirmation_rebuilds_slot_based_pending_items tests/test_menu_order_consistency.py::test_order_handler_confirmation_handles_string_pending_items_without_crash tests/test_chat_service_faq_bank.py::test_full_kb_order_without_requires_ticket_keeps_order_flow tests/test_chat_service_faq_bank.py::test_full_kb_table_booking_with_requires_ticket_keeps_booking_flow`
- Result: `4 passed`.
- `python -m pytest -q tests/test_ticketing_local_mode.py tests/test_ticketing_router_service.py tests/test_complaint_ticket_enrichment.py tests/test_ticketing_service_payload.py`
- Result: `11 passed`.

## 2026-02-25 00:15:00 - Order Quantity Flow Misroute Fix (No Ticket Hijack)

- Fixed a remaining conversational bug in food-order flow:
  - Scenario: user selects dish, bot asks quantity, user replies with number (for example `3`), and flow incorrectly jumped to complaint/ticketing.

- Root cause:
  - In full-KB passthrough mode, ticket-routing happened before robust order slot progression.
  - Spurious model complaint/ticket signals could hijack quantity collection turns.

- Fixes implemented in `services/chat_service.py`:
  - Added generalized guard helpers:
    - `_is_order_pending_action(...)`
    - `_looks_like_strong_ticket_issue_marker(...)`
  - Hardened `_should_route_full_kb_ticketing_handler(...)`:
    - while in order pending actions, ticket routing is blocked unless user explicitly switches to complaint/ticket intent.
  - Added passthrough-mode deterministic correction for quantity collection:
    - for `collect_order_quantity`, numeric reply now stays in order flow,
    - transitions to `collect_order_addons` with human-style prompt,
    - avoids complaint/ticket detour unless explicit issue/ticket switch is detected.

- Added regression test:
  - `tests/test_chat_service_faq_bank.py`
    - `test_full_kb_order_quantity_reply_ignores_spurious_complaint_ticket_route`

### Validation
- `python -m pytest -q tests/test_chat_service_faq_bank.py::test_full_kb_order_quantity_reply_ignores_spurious_complaint_ticket_route tests/test_chat_service_faq_bank.py::test_full_kb_order_without_requires_ticket_keeps_order_flow tests/test_chat_service_faq_bank.py::test_full_kb_requires_ticket_true_routes_order_to_complaint_handler tests/test_chat_service_faq_bank.py::test_full_kb_table_booking_with_requires_ticket_keeps_booking_flow`
- Result: `4 passed`.
- `python -m pytest -q tests/test_menu_order_consistency.py::test_order_handler_confirmation_rebuilds_slot_based_pending_items tests/test_ticketing_local_mode.py tests/test_ticketing_router_service.py tests/test_complaint_ticket_enrichment.py tests/test_ticketing_service_payload.py`
- Result: `12 passed`.

## 2026-02-25 00:40:00 - Lumira-Style Ticket Creation Added for Confirmed Order + Booking Flows

- Implemented backend ticket creation for transactional flows (as in Lumira), while preserving conversational UX:
  - `handlers/order_handler.py`
    - On final order confirmation, now creates operational ticket via `ticketing_service` (non-blocking).
    - Works for both DB order path and fallback order path.
    - Added metadata propagation: `ticket_created`, `ticket_id`, `ticket_source`, `ticket_status`, category/priority, API status/response.
  - `handlers/booking_handler.py`
    - On final booking confirmation, now creates operational ticket via `ticketing_service` (non-blocking).
    - Added same ticket metadata propagation into booking result metadata.
    - Converted booking confirmation helper to async to support API call cleanly.

- Behavioral intent:
  - Booking/order chat remains human and flow-first.
  - Ticket creation happens in backend at confirmed transactional step.
  - Ticket API failure does not block order/booking confirmation response.

- Added regression tests:
  - `tests/test_menu_order_consistency.py`
    - `test_order_handler_confirmation_creates_operational_ticket_metadata`
  - `tests/test_manual_issue_fixes.py`
    - `test_booking_handler_confirmation_creates_operational_ticket_metadata`

### Validation
- `python -m pytest -q tests/test_menu_order_consistency.py::test_order_handler_confirmation_creates_operational_ticket_metadata tests/test_manual_issue_fixes.py::test_booking_handler_confirmation_creates_operational_ticket_metadata tests/test_menu_order_consistency.py::test_order_handler_confirmation_rebuilds_slot_based_pending_items tests/test_manual_issue_fixes.py::test_booking_handler_requires_party_size_and_time`
- Result: `4 passed`.
- `python -m pytest -q tests/test_chat_service_faq_bank.py::test_full_kb_order_quantity_reply_ignores_spurious_complaint_ticket_route tests/test_ticketing_local_mode.py tests/test_ticketing_router_service.py tests/test_complaint_ticket_enrichment.py tests/test_ticketing_service_payload.py`
- Result: `12 passed`.

## 2026-02-25 01:05:00 - Complaint Ticketing UX Fix (Auto-Create + Context-Aware Issue Extraction)

- Addressed user-facing complaint-ticketing pain points:
  - Removed unnecessary "Should I create this ticket now?" prompt for actionable complaint flows.
  - Complaint tickets now auto-create once required details are available (especially room number), matching Lumira-style backend behavior.
  - Added contextual issue enrichment so generic messages (e.g., "its not there in my room") inherit previous user context (e.g., "hairdryer") for better ticket issue quality.

- Implemented in `handlers/complaint_handler.py`:
  - `collect_ticket_room_number` path is now async and supports immediate ticket creation.
  - New setting-gated behavior:
    - `ticketing_auto_create_on_actionable` (default enabled).
  - Added unified ticket creation helper:
    - `_create_ticket_from_pending(...)`
    - used by room-number collection, explicit confirmation path, and auto-create path.
  - Added issue-context helpers:
    - `_resolve_issue_text(...)`
    - `_get_previous_user_message(...)`
  - Existing create-vs-update smart routing preserved; auto-create does not bypass duplicate checks.

- Added configuration support:
  - `config/settings.py`:
    - `ticketing_auto_create_on_actionable: bool = True`
  - `.env.example`:
    - `TICKETING_AUTO_CREATE_ON_ACTIONABLE=true`

- Tests updated/added:
  - `tests/test_complaint_ticket_enrichment.py`
    - updated existing switch-issue test to explicitly disable auto-create for legacy confirmation scenario
    - added `test_collect_ticket_room_number_auto_creates_ticket_when_enabled`
    - added `test_issue_resolution_uses_previous_user_context_for_generic_issue`

### Validation
- `python -m pytest -q tests/test_complaint_ticket_enrichment.py tests/test_manual_issue_fixes.py::test_booking_handler_confirmation_creates_operational_ticket_metadata tests/test_menu_order_consistency.py::test_order_handler_confirmation_creates_operational_ticket_metadata tests/test_chat_service_faq_bank.py::test_full_kb_order_quantity_reply_ignores_spurious_complaint_ticket_route`
- Result: `8 passed`.
- `python -m pytest -q tests/test_ticketing_local_mode.py tests/test_ticketing_router_service.py tests/test_ticketing_service_payload.py`
- Result: `8 passed`.

## 2026-02-25 01:30:00 - Full-KB Confirmed Booking/Order Now Persist Backend Tickets (No Response Text Change)

- Fixed full-KB passthrough gap where confirmed transactional replies looked correct but no ticket was persisted.
- Implemented backend ticket persistence directly in `services/chat_service.py` full-KB flow:
  - Added `_maybe_create_full_kb_transaction_ticket(...)`.
  - Trigger condition:
    - previous pending action is `confirm_order` or `confirm_booking`
    - effective intent is `confirmation_yes`
    - next state is terminal (`completed` or `idle`)
  - Creates Lumira-style ticket payload using `ticketing_service` without altering assistant response content.
  - Writes ticket metadata into assistant/chat response metadata for memory/ops observability.

- Added transactional issue builders:
  - `_build_order_ticket_issue_from_pending(...)`
  - `_build_booking_ticket_issue_from_pending(...)`
  - These derive actionable issue text from pending slot context (item/quantity/total, service/party/time/date).

- Integrated in both full-KB paths:
  - passthrough branch
  - non-passthrough branch

- Preserved user-visible response:
  - response text remains exactly what LLM generated for confirmation turns.
  - only backend ticket logic/metadata added.

- Tests added:
  - `tests/test_chat_service_faq_bank.py`
    - `test_full_kb_confirm_booking_creates_backend_ticket_without_changing_response`
    - `test_full_kb_confirm_order_creates_backend_ticket_without_changing_response`

### Validation
- `python -m pytest -q tests/test_chat_service_faq_bank.py::test_full_kb_confirm_booking_creates_backend_ticket_without_changing_response tests/test_chat_service_faq_bank.py::test_full_kb_confirm_order_creates_backend_ticket_without_changing_response tests/test_chat_service_faq_bank.py::test_full_kb_order_quantity_reply_ignores_spurious_complaint_ticket_route`
- Result: `3 passed`.
- `python -m pytest -q tests/test_ticketing_local_mode.py tests/test_ticketing_router_service.py tests/test_ticketing_service_payload.py tests/test_complaint_ticket_enrichment.py`
- Result: `13 passed`.

## 2026-02-25 02:05:00 - Local Ticket CSV Ledger Added (Lumira-Style Fields, No DB Writes)

- Implemented local CSV persistence for ticket creation to mirror Lumira operational ticket output while staying DB-safe:
  - `services/ticketing_service.py`
    - Added CSV ledger support with schema-aligned fields (modeled from `GHN_OPERATION_TICKETS` plus local metadata):
      - `id`, `ticket_id`, `session_id`, `guest_id`, `entity_id`, `fms_entity_id`, `issue`, `message_id`, `sentiment_score`, `department_alloc`, `priority`, `category`, `phase`, `status`, `sla_due`, `escalation_stage`, `created_at`, `updated_at`, `room_number`, `message`, `customer_feedback`, `manager_notes`, `assignee_notes`, `assigned_to`, `assigned_id`, `customer_rating`, `department_id`, `sla_due_at`, `sla_duration_minutes`, `ticket_auto_assign`, `closed_at`, `outlet_id`, `input_tokens`, `output_tokens`, `total_tokens`, `cost`, `sub_category`, `guest_name`, `compensation_type`, `compensation_currency`, `compensation_amount`, `group_id`, `ticket_source`, `cancelled_notes`.
      - Local audit columns: `mode`, `local_created_at_utc`, `local_updated_at_utc`, `payload_json`, `response_json`.
    - Added configurable CSV path support:
      - `TICKETING_LOCAL_CSV_FILE` (optional)
      - If unset, defaults beside JSON store (for example `local_tickets.json` -> `local_tickets.csv`).
    - On each local ticket create:
      - Keeps existing JSON write behavior.
      - Appends one CSV row with full normalized payload details.
      - Adds `csv_written` + `csv_path` in ticket create response metadata.
    - Added bootstrap behavior:
      - If CSV does not exist yet but JSON store has historical tickets, CSV auto-backfills from existing JSON records before appending new rows.

- Config updates:
  - `config/settings.py`
    - Added `ticketing_local_csv_file: str = ""`
  - `.env.example`
    - Added `TICKETING_LOCAL_CSV_FILE=`

- Tests updated:
  - `tests/test_ticketing_local_mode.py`
    - `test_local_mode_create_and_update_persist_to_local_store` now also verifies CSV row append and key values.
    - Added `test_local_mode_bootstraps_csv_with_existing_json_tickets` for JSON-to-CSV backfill on first write.

### Validation
- `python -m pytest -q tests/test_ticketing_local_mode.py tests/test_ticketing_service_payload.py tests/test_ticketing_router_service.py`
- Result: `9 passed`.
- `python -m pytest -q tests/test_chat_service_faq_bank.py::test_full_kb_confirm_booking_creates_backend_ticket_without_changing_response tests/test_chat_service_faq_bank.py::test_full_kb_confirm_order_creates_backend_ticket_without_changing_response`
- Result: `2 passed`.

## 2026-02-25 02:12:00 - Ticketing Test Guide Sync For CSV Ledger

- Updated `ticketing_integration_test_guide.md` to include CSV ledger verification:
  - Added optional env var docs: `TICKETING_LOCAL_CSV_FILE`
  - Added validation checks for per-ticket CSV append behavior in local mode.

## 2026-02-25 02:45:00 - Ticketing Agent Layer Added For Full-KB Flow

- Implemented dedicated ticketing decision layer:
  - Added `services/ticketing_agent_service.py`
    - New `TicketingAgentService.decide(...)` and `TicketingAgentDecision`.
    - Decision goals:
      - Keep order/table booking slot collection stable.
      - Route actionable complaint/room-need turns to complaint ticketing flow.
      - Avoid spurious complaint hijacks from noisy model labels.
    - Supports explicit ticket commands, pending ticket actions, room-service actionable detection, and human-request escalation routing.

- Integrated decision layer into chat orchestration:
  - Updated `services/chat_service.py`:
    - `_maybe_route_full_kb_ticketing_handler(...)` now uses `ticketing_agent_service.decide(...)` as the first gate.
    - Routing now respects transactional deferral:
      - `order_food` / `table_booking` do not jump to complaint handler just because `requires_ticket=true`; ticket remains backend-confirmation driven.
    - Added decision metadata in routed responses:
      - `full_kb_ticketing_agent_route`
      - `full_kb_ticketing_agent_decision_reason`
      - `full_kb_ticketing_agent_decision_source`

- Prompt alignment for human-first ticket flow:
  - Updated `services/full_kb_llm_service.py` system rules:
    - Reinforced service-first conversational style.
    - Explicitly instructs: backend handles ticket creation after final transactional confirmation.
    - For complaints/room-needs, gather missing operational details naturally and proceed.

- Tests added/updated:
  - Added `tests/test_ticketing_agent_service.py`:
    - order intent does not auto-activate ticketing agent
    - actionable room-service request activates complaint route
    - room-service informational query does not activate ticketing
    - complaint intent always activates ticketing
  - Updated `tests/test_chat_service_faq_bank.py`:
    - replaced order hijack expectation with non-hijack expectation:
      - `test_full_kb_requires_ticket_true_does_not_hijack_order_flow`
    - added:
      - `test_full_kb_room_service_info_query_with_requires_ticket_false_skips_ticketing_agent`

### Validation
- `python -m pytest -q tests/test_ticketing_agent_service.py tests/test_chat_service_faq_bank.py::test_full_kb_requires_ticket_true_does_not_hijack_order_flow tests/test_chat_service_faq_bank.py::test_full_kb_room_service_request_routes_to_complaint_handler tests/test_chat_service_faq_bank.py::test_full_kb_room_service_info_query_with_requires_ticket_false_skips_ticketing_agent tests/test_chat_service_faq_bank.py::test_full_kb_confirm_booking_creates_backend_ticket_without_changing_response tests/test_chat_service_faq_bank.py::test_full_kb_confirm_order_creates_backend_ticket_without_changing_response tests/test_ticketing_local_mode.py tests/test_ticketing_service_payload.py tests/test_ticketing_router_service.py`
- Result: `18 passed`.
- `python -m pytest -q tests/test_complaint_ticket_enrichment.py tests/test_menu_order_consistency.py tests/test_manual_issue_fixes.py`
- Result: `26 passed`.
- `python -m pytest -q tests/test_chat_service_faq_bank.py::test_full_kb_confirm_booking_creates_backend_ticket_without_changing_response tests/test_chat_service_faq_bank.py::test_full_kb_confirm_order_creates_backend_ticket_without_changing_response tests/test_ticketing_agent_service.py`
- Result: `6 passed`.

## 2026-02-25 03:20:00 - Generalized Ticketing + Guest DB Room Auto-Fill

- Extended ticketing generalization beyond fixed booking/order paths:
  - `handlers/complaint_handler.py`
    - Added dynamic sub-category inference (`_detect_sub_category`) for wider operational cases:
      - `table_booking`, `room_booking`, `order_food`, `housekeeping`, `amenities`, `laundry`, `maintenance`, `billing`, `transport`, `spa`.
    - Added guest DB hydration in enrichment path:
      - `_hydrate_guest_details_from_db(...)`
      - Auto-fills `room_number`, `guest_name`, `guest_id` from legacy FF guest table context before asking the user.
      - Updates integration memory (`pending_data['_integration']`) so downstream ticketing payloads stay complete.

- Added legacy guest-profile DB lookup utility:
  - `integrations/lumira_ticketing_repository.py`
    - New `fetch_guest_profile(...)` on `GHN_FF_PROD.GHN_FF_GUEST_INFO`:
      - supports guest-id and phone-based lookup
      - prefers current-stay match (`NOW() BETWEEN CHECK_IN_DATE AND CHECK_OUT_DATE`)
      - normalizes phone matching for formatted numbers.

- Made room-service flow operational-ticket aware (human-style response preserved):
  - `handlers/room_service_handler.py`
    - Auto-resolves room number from guest DB when missing (before prompting user).
    - Auto-creates backend ticket for actionable room-service outcomes without asking technical ticket questions.
    - Keeps same natural user-facing text; ticketing is backend metadata side-effect.
    - Adds ticket metadata (`ticket_created`, `ticket_id`, `ticket_sub_category`, etc.) when created.

- Tests added/updated:
  - Added `tests/test_room_service_ticketing.py`:
    - DB room auto-resolve + ticket create for room-service request
    - info-only room-service query does not create ticket.
  - Updated `tests/test_complaint_ticket_enrichment.py`:
    - added DB guest-profile hydration test for room-number auto-fill.

### Validation
- `python -m pytest -q tests/test_ticketing_agent_service.py tests/test_room_service_ticketing.py tests/test_complaint_ticket_enrichment.py tests/test_chat_service_faq_bank.py::test_full_kb_room_service_request_routes_to_complaint_handler tests/test_chat_service_faq_bank.py::test_full_kb_room_service_info_query_with_requires_ticket_false_skips_ticketing_agent tests/test_chat_service_faq_bank.py::test_full_kb_confirm_booking_creates_backend_ticket_without_changing_response tests/test_chat_service_faq_bank.py::test_full_kb_confirm_order_creates_backend_ticket_without_changing_response tests/test_ticketing_local_mode.py tests/test_ticketing_service_payload.py tests/test_ticketing_router_service.py`
- Result: `25 passed`.
- `python -m pytest -q tests/test_menu_order_consistency.py tests/test_manual_issue_fixes.py`
- Result: `21 passed`.

## 2026-02-25 03:45:00 - Full-KB Greeting Misfire Fix (Deterministic Shortcut)

- Investigated reported regression where `hi` returned KB-miss fallback despite full-KB mode being enabled.
- Root cause confirmed from `logs/detailedsteps.log`:
  - full-KB pipeline loaded KB successfully (`kb_chars` present),
  - but model returned `intent=unclear` + fallback sentence for greeting turn.

- Implemented deterministic full-KB-safe greeting/identity shortcuts in `services/chat_service.py`:
  - Added `_match_greeting_response(...)`.
  - In `_process_full_kb_llm_message(...)`, added early shortcut handling for:
    - simple greetings (`hi`, `hello`, etc.) -> returns configured welcome message
    - identity queries (`who are you`) via existing identity matcher
  - Shortcuts bypass LLM for these stable intents and preserve full-KB metadata path:
    - `response_source=full_kb_shortcut`
    - `full_kb_shortcut_type=greeting|identity`

- This restores expected “behaves like before” greeting UX while keeping full-KB mode active for other turns.

- Tests added/updated:
  - `tests/test_chat_service_faq_bank.py`
    - `test_match_greeting_response_returns_welcome_message`
    - `test_full_kb_simple_greeting_uses_shortcut_without_llm`

### Validation
- `python -m pytest -q tests/test_chat_service_faq_bank.py::test_match_greeting_response_returns_welcome_message tests/test_chat_service_faq_bank.py::test_full_kb_simple_greeting_uses_shortcut_without_llm tests/test_chat_service_faq_bank.py::test_full_kb_confirm_booking_creates_backend_ticket_without_changing_response tests/test_chat_service_faq_bank.py::test_full_kb_confirm_order_creates_backend_ticket_without_changing_response tests/test_ticketing_agent_service.py tests/test_room_service_ticketing.py`
- Result: `10 passed`.

## 2026-02-25 04:05:00 - Identity Typo Robustness + Old-Style Concierge Response

- Investigated user-reported mismatch against old bot behavior:
  - Greeting (`hi`) worked, but typoed identity query (`who arre u`) fell to `unclear` + KB-miss fallback.
  - This caused UI classification/confidence bubbles to look wrong for a basic identity question.

- Implemented robust deterministic identity detection in `services/chat_service.py`:
  - Enhanced `_match_identity_response(...)` to support natural/typo variants:
    - examples: `who arre u`, `who r u`, `who are u`, `who ru`, `what's your name`.
  - Added token/fuzzy logic (SequenceMatcher) for resilient matching.
  - Updated deterministic identity reply style to align with old concierge tone:
    - `I am {bot_name}, your concierge assistant at {hotel_name} in {city}. How may I assist you today?`

- Full-KB identity shortcut now correctly handles typoed identity asks before LLM call,
  preserving consistent intent/confidence behavior.

- Tests added:
  - `tests/test_chat_service_faq_bank.py`
    - `test_match_identity_response_handles_typo_variant`
    - `test_full_kb_identity_typo_uses_shortcut_without_llm`

### Validation
- `python -m pytest -q tests/test_chat_service_faq_bank.py::test_match_identity_response_handles_typo_variant tests/test_chat_service_faq_bank.py::test_full_kb_identity_typo_uses_shortcut_without_llm tests/test_chat_service_faq_bank.py::test_full_kb_simple_greeting_uses_shortcut_without_llm tests/test_chat_service_faq_bank.py::test_full_kb_confirm_booking_creates_backend_ticket_without_changing_response tests/test_chat_service_faq_bank.py::test_full_kb_confirm_order_creates_backend_ticket_without_changing_response tests/test_ticketing_agent_service.py tests/test_room_service_ticketing.py`
- Result: `11 passed`.
## 2026-02-24 - Ticketing UX + Phase Logic Fixes (Latest)

- Fixed complaint/facility flow to stay human-facing while keeping ticket creation in backend.
  - Updated `ComplaintHandler` success response to avoid explicit ticket prompts and escalation questions.
  - Bot now responds with team-action language (for example housekeeping/engineering/front desk) after ticket creation.
  - Room-number collection prompt is now service-first (no technical ticket wording).
  - Files: `handlers/complaint_handler.py`

- Fixed wrong phase for table-booking tickets.
  - Changed booking handler ticket phase from `pre_booking` to `during_stay`.
  - Changed full-KB transactional booking ticket phase from `pre_booking` to `during_stay`.
  - Files: `handlers/booking_handler.py`, `services/chat_service.py`

- Fixed ticketing-agent misrouting during order/booking slot collection.
  - Root cause: `complaint` intent was being prioritized before transaction slot-collection guard.
  - Updated routing precedence so quantity/detail replies (for example `3`) do not get hijacked into complaint ticketing unless there is a strong explicit issue marker.
  - File: `services/ticketing_agent_service.py`

- Added/updated tests for regression coverage.
  - Complaint auto-create now expects backend ticket creation with no escalation-confirmation pending state.
  - Booking ticket payload assertions now enforce `phase=\"during_stay\"` in both booking handler and full-KB transaction path tests.
  - Files:
    - `tests/test_complaint_ticket_enrichment.py`
    - `tests/test_manual_issue_fixes.py`
    - `tests/test_chat_service_faq_bank.py`

- Validation results:
  - `pytest -q tests/test_chat_service_faq_bank.py::test_full_kb_order_quantity_reply_ignores_spurious_complaint_ticket_route` -> passed
  - `pytest -q tests/test_ticketing_agent_service.py` -> passed
  - `pytest -q tests/test_complaint_ticket_enrichment.py tests/test_manual_issue_fixes.py::test_booking_handler_confirmation_creates_operational_ticket_metadata tests/test_chat_service_faq_bank.py::test_full_kb_confirm_booking_creates_backend_ticket_without_changing_response` -> passed
  - `pytest -q tests/test_complaint_ticket_enrichment.py tests/test_room_service_ticketing.py tests/test_chat_service_faq_bank.py -k "ticket or full_kb_confirm_booking_creates_backend_ticket_without_changing_response"` -> passed (17 passed, 19 deselected)
## 2026-02-24 - Suggestion Chip Filter + CSV Sync Reliability

- Clarified UI behavior per request:
  - Kept intent/confidence display untouched.
  - Removed only `Greeting` and `Knowledge Query` from user suggestion chips.
  - Implemented at two layers so it works for both config quick-actions and LLM-provided suggestions.
  - Files: `services/config_service.py`, `services/chat_service.py`

- Fixed local ticket CSV drift issue:
  - Root cause observed: `local_tickets.csv` can be file-locked by another process, so append writes fail while JSON continues updating.
  - Strengthened local ticket persistence by rewriting CSV from JSON store on every local create/update.
  - This guarantees eventual consistency: once CSV is unlocked, next ticket operation syncs all rows from JSON.
  - File: `services/ticketing_service.py`

- Complaint severity/routing quality improvements:
  - Priority detection upgraded: pest/cockroach now escalates to `high`; severe safety markers map to `critical`.
  - Post-ticket team message now maps pest issues to housekeeping in guest-facing text.
  - Added pest keyword sub-category mapping for better downstream routing.
  - File: `handlers/complaint_handler.py`

- Tests added/validated:
  - Added regression test to ensure quick actions exclude Greeting/Knowledge Query.
  - `tests/test_config_service.py::test_quick_actions_exclude_greeting_and_knowledge_query` passed.
  - `tests/test_ticketing_local_mode.py` passed.
  - `tests/test_complaint_ticket_enrichment.py tests/test_chat_service_faq_bank.py -k "ticket"` passed.
## 2026-02-24 - Ticketing Plugin Mode Alignment + CSV Lock Handling

- Aligned ticketing to plugin-agent behavior so core chat responses remain unchanged.
  - Added plugin toggles:
    - `TICKETING_PLUGIN_ENABLED`
    - `TICKETING_PLUGIN_TAKEOVER_MODE`
  - Default takeover is disabled (`false`) so bot replies follow core LLM flow and ticketing runs as backend side-effect.
  - Files: `config/settings.py`, `.env`, `.env.example`

- Full-KB flow change:
  - Ticketing handler takeover (`_maybe_route_full_kb_ticketing_handler`) now runs only when takeover mode is explicitly enabled.
  - Added non-intrusive plugin ticket creation path (`_maybe_create_full_kb_plugin_ticket`) that creates tickets in backend without changing response text.
  - Keeps transactional ticket creation for confirmed order/booking and adds non-transactional complaint/room-service plugin ticket path.
  - Files: `services/chat_service.py`

- Ticketing enable/disable toggle enforced centrally:
  - `ticketing_service.is_ticketing_enabled()` now respects `TICKETING_PLUGIN_ENABLED`.
  - File: `services/ticketing_service.py`

- CSV sync hardening:
  - Local ticket CSV sync now rewrites from JSON source-of-truth and handles locked `local_tickets.csv` by writing to `local_tickets_mirror.csv` fallback.
  - Response metadata now includes `csv_primary_synced` and actual `csv_path` used.
  - File: `services/ticketing_service.py`

- Suggestion-chip alignment retained:
  - Quick actions exclude `Greeting` / `Knowledge Query` while intent/confidence UI remains unchanged.
  - Files: `services/config_service.py`, `services/chat_service.py`

- Tests updated/added:
  - Added plugin-mode regression test: backend ticket is created without handler takeover and without changing response text.
  - Updated handler-takeover tests to explicitly enable takeover mode.
  - Added quick-actions exclusion regression test.
  - Files: `tests/test_chat_service_faq_bank.py`, `tests/test_config_service.py`

- Validation:
  - `pytest -q tests/test_chat_service_faq_bank.py -k "ticketing or full_kb_plugin_ticketing_creates_backend_ticket_without_changing_response or full_kb_confirm_booking_creates_backend_ticket_without_changing_response or full_kb_confirm_order_creates_backend_ticket_without_changing_response"` -> passed
  - `pytest -q tests/test_ticketing_local_mode.py tests/test_config_service.py::test_quick_actions_exclude_greeting_and_knowledge_query` -> passed
  - `pytest -q tests/test_ticketing_agent_service.py tests/test_complaint_ticket_enrichment.py -k "ticket"` -> passed

- Runtime note:
  - Current primary CSV file is locked by another process; sync successfully wrote `data/ticketing/local_tickets_mirror.csv` with full up-to-date rows from JSON store.

## 2026-02-25 23:55:00 - Ticketing Agent Reliability + Config-Driven Spa Creation (No Hardcoding)

- Investigated why spa did not always create a ticket even when admin ticketing case included `spa booking`.
  - Root cause: configured transactional cases were always deferred, even when assistant response already committed staff action (for example, "I'll forward this to our staff").
  - Additional issue fixed earlier in same cycle: case-match LLM `no-match` could skip deterministic fallback and cause missed ticket creation.

- Implemented config-driven ticketing behavior updates:
  - `services/ticketing_agent_service.py`
    - `match_configured_case_async(...)` now falls back to deterministic matching when case-match LLM returns no match.
    - Added explicit case-match logs (`source=llm` or `source=fallback`) for root-cause visibility.
    - Added rule: if a configured transactional case matches AND assistant response implies staff action, activate backend ticketing immediately.
    - Kept confirmation deferral when order/booking slot collection is still in progress.
    - Final behavior is case-driven + state-driven, not spa-specific hardcoded routing.
  - `services/chat_service.py`
    - Plugin ticketing path now evaluates ticketing-agent decision before generic intent early exits.
    - Complaint-routed plugin tickets are allowed without room/identity gating so urgent operational issues are not dropped.

- Runtime/default alignment:
  - Enabled deterministic fallback in runtime env for case-matching resilience:
    - `.env`: `TICKETING_CASE_MATCH_FALLBACK_ENABLED=true`
  - `config/settings.py` default also uses fallback enabled.

- Tests added/updated:
  - `tests/test_ticketing_agent_service.py`
    - Added regression: fallback still matches configured case when LLM returns no-match.
    - Added regression: transactional configured case with staff-forward response activates ticketing (`configured_transaction_case_staff_action`).
  - `tests/test_chat_service_faq_bank.py`
    - Added regression: complaint ticket can be created even without room number in plugin mode.
    - Added regression: ticketing-agent decision is evaluated on greeting turn (no ticket created).
    - Added regression: spa request that is explicitly forwarded to staff creates backend ticket.

### Validation
- `pytest tests/test_ticketing_agent_service.py tests/test_ticketing_local_mode.py -q`
  - Result: `26 passed`.
- `pytest tests/test_chat_service_faq_bank.py -q -k "full_kb_plugin_ticketing_creates_backend_ticket_without_changing_response or full_kb_plugin_ticketing_creates_complaint_without_room_number or full_kb_plugin_ticketing_evaluates_agent_for_greeting_turn or full_kb_plugin_ticketing_skips_human_request_without_matching_case or full_kb_plugin_ticketing_creates_human_handoff_ticket_with_matching_case or full_kb_plugin_ticketing_defers_spa_case_until_confirmation or full_kb_confirm_booking_creates_backend_ticket_without_changing_response or full_kb_confirm_transport_like_booking_skips_ticket_without_matching_case"`
  - Result: `8 passed, 32 deselected`.
- `pytest tests/test_ticketing_agent_service.py -q`
  - Result: `22 passed`.
- `pytest tests/test_chat_service_faq_bank.py -q -k "plugin_ticketing_defers_spa_case_until_confirmation or plugin_ticketing_creates_spa_ticket_when_forwarded_to_staff or plugin_ticketing_creates_backend_ticket_without_changing_response or plugin_ticketing_creates_complaint_without_room_number or plugin_ticketing_evaluates_agent_for_greeting_turn"`
  - Result: `5 passed, 36 deselected`.
- `python -m py_compile services/ticketing_agent_service.py services/chat_service.py tests/test_chat_service_faq_bank.py tests/test_ticketing_agent_service.py`
  - Result: passed.

## 2026-03-05 17:20:00 - Phase-Aware UI + Phase-Correlated Ticketing Controls (Service-Agent Integration Paused)

- Implemented phase selection in chat test UI and request metadata propagation:
  - Added phase dropdown to chat test page (`pre_booking`, `pre_checkin`, `during_stay`, `post_checkout`).
  - Chat API payload now sends `metadata.phase` from selected phase.
  - Files: `templates/chat.html`, `static/js/chat.js`, `static/css/chat.css`.

- Extended admin phase workspace with per-service ticketing control:
  - Added per-service `ticketing_enabled` toggle in Phase -> Services list.
  - Added ticketing checkbox in "Create New Service For This Phase".
  - Prebuilt phase service add flow now persists `ticketing_enabled`.
  - File: `templates/admin.html`.

- Added ticketing association visibility in Tools tab:
  - New "Ticketing Associations By Phase" panel under Tools & Workflows.
  - Shows all active phase-mapped services with `ticketing_enabled=true`, grouped by phase.
  - File: `templates/admin.html`.

- Added backend/model support for phase ticketing toggle:
  - `AddService` now accepts `ticketing_enabled`.
  - Service normalization persists `ticketing_enabled`.
  - Prebuilt phase templates default to `ticketing_enabled=true`.
  - Files: `api/routes/admin.py`, `services/config_service.py`.

- Runtime ticketing behavior now correlates with phase service toggle:
  - Expanded phase-managed service match to include informational queries (not only explicit action verbs).
  - Added gate `phase_service_ticketing_disabled`:
    - when request is in same phase but service has `ticketing_enabled=false`, backend ticket creation is skipped.
  - Applied in both full-KB ticket creation paths:
    - transactional confirmation path
    - plugin/non-transaction ticket path
  - File: `services/chat_service.py`.

- Tests added/updated:
  - `tests/test_policy_and_service_runtime.py`
    - info-query out-of-phase mismatch detection
    - plugin ticket skip when `ticketing_enabled=false`
    - transaction ticket skip when `ticketing_enabled=false`
  - `tests/test_config_service.py`
    - prebuilt templates carry `ticketing_enabled=true`
    - service-level `ticketing_enabled` persists.

### Validation
- `pytest tests/test_policy_and_service_runtime.py -q`
  - Result: `40 passed`.
- `pytest tests/test_config_service.py -q`
  - Result: `13 passed`.
- `pytest tests/test_chat_service_faq_bank.py -k "phase_gate_declines_out_of_phase_request" -q`
  - Result: `1 passed`.
- `python -m py_compile api/routes/admin.py services/config_service.py services/chat_service.py`
  - Result: passed.
- `node --check static/js/chat.js`
  - Result: passed.

### Note
- Service-agent integration is intentionally paused in this cycle as requested; only non-service-agent phase/ticketing integration work was implemented.

---

## Latest Update (March 16, 2026)

### Feature: Suggestion Bubbles — Full Overhaul

**Goal**: The auto-suggestion chips shown after every bot response were generating random, context-blind, and bot-perspective suggestions (e.g. "Ask about room types", "Here's my full name and details", "7 PM", "101"). Overhauled the full suggestion pipeline to be contextual, grounded in the knowledge base and active service, and written purely as natural guest messages.

---

#### Root Cause Analysis

Three compounding problems were identified:

1. **Layer 1 (chat_service.py) always fired first and returned generic chips** — so Layer 2 (the LLM suggestion agent with full context) never ran. Suggestions like "Ask another question", "Show options", service names from the catalog, and hardcoded intent-based strings were returned regardless of what the bot actually said.

2. **Layer 3 (fallback endpoint) was being called by the frontend for every response**, ignoring `data.suggested_actions` already returned by the orchestrator. The endpoint only received `last_bot_message` + `user_message`, with no knowledge of phase, active service, conversation state, or history.

3. **LLM prompt framing** across all layers was producing bot-perspective labels ("Ask about X", "View services") instead of actual guest messages, because `response_contract` specified `"2-5 words action label"` — and there was no explicit first-person voice instruction or guard against suggesting personal/unique data.

---

#### Changes Made

**`static/js/chat.js`**
- If `data.suggested_actions` is non-empty (set by orchestrator with full context), use them directly — no endpoint call.
- Only call `/api/chat/suggestions` as a fallback when orchestrator returns nothing.
- Pass `session_id` in the fallback request so the endpoint can load live session context.

**`api/routes/chat.py` — `/api/chat/suggestions` endpoint**
- Added `session_id` and `current_phase` fields to `SuggestionsRequest`.
- When `session_id` is provided: loads live session context via `context_manager.get_context()` to extract conversation state, `pending_action`, phase, and active service.
- Looks up the active service from `config_service.get_service(pending_action)` — includes service name and profile in the payload.
- Phase description loaded dynamically from `config_service.get_journey_phases()` — nothing hardcoded.
- Phase services loaded from `config_service.get_phase_services(phase_id)` — reflects live config.
- KB text loaded via `config_service.get_full_kb_text(max_chars=5000)` — LLM infers topics itself, no hardcoded topic list.
- Recent conversation history (last 6 messages, 2000 char cap) extracted from context and included in payload.
- Full `context_payload` passed to LLM as JSON: `last_bot_message`, `user_message`, `conversation_history`, `conversation_state`, `journey_phase`, `active_service`, `available_phase_services`, `knowledge_base_excerpt`.
- System prompt rewritten with explicit reasoning order: bot message first → history → active service → KB/phase.

**`services/llm_orchestration_service.py` — `_run_next_action_suggestion_agent`**
- `response_contract` changed from `"2-5 words action label"` → `"what the guest would type next"` — stops the LLM writing bot-perspective labels.
- System prompt rewritten with explicit reasoning order: read `assistant_response` first → `history` → `service` + `decision.missing_fields` → `selected_phase`.
- Added voice enforcement with bad/good examples.
- Added unique-values principle (see below).

**`services/chat_service.py` — `_build_contextual_suggested_actions` + `_get_suggested_actions`**
- Both methods now only return deterministic chips for functional states that require them:
  - `AWAITING_CONFIRMATION` → `[confirmation_phrase, "cancel"]`
  - `ESCALATED` → `["Return to bot"]`
  - Confirm-type `pending_action` values → `[confirmation_phrase, "cancel"]`
- Everything else returns `[]` — this unblocks Layer 2 from running in all normal conversation flows.
- Removed: generic service catalog chips, intent-based fallbacks ("Ask another question", "Show options", "Talk to human"), `get_quick_actions()` calls, and all bot-perspective strings.

**`handlers/booking_handler.py`**
- Removed specific-value chips that guests must supply themselves:
  - `["Kadak", "In-Room Dining"]` → `["cancel"]`
  - `["2", "4", "6"]` (party size) → `["cancel"]`
  - `["7 PM", "8 PM", "9 PM"]` (times) → `["cancel"]`

**`handlers/complaint_handler.py`**
- Removed specific-value chips:
  - `["101", "202", "305"]` (room numbers) → `["cancel"]`
  - `["101", "202", "A-12"]` (room number format examples) → `["cancel"]`

---

#### Prompt Rules Added to All LLM Suggestion Layers

All LLM prompts (Layer 2 + fallback endpoint) now enforce:

1. **Reasoning order**: `assistant_response`/`last_bot_message` → `history` → active service/missing fields → phase/KB. Suggestions must be a direct natural response to what the bot just said.

2. **First-person voice**: Every suggestion must be a natural guest message — exactly what they would type.
   - Bad: `"Ask about room types"`, `"View services"`, `"Show options"`, `"Share details"`
   - Good: `"What room types do you have?"`, `"What services are available?"`, `"Can I see my options?"`

3. **No unique personal values**: Never suggest any value unique to the individual guest — names, room numbers, phone numbers, email addresses, flight numbers, dates, times, booking references, quantities, party sizes, prices, or any personal/context-specific data.

4. **No data-submission messages**: Never suggest a message where the guest is offering or providing their personal information, even without stating the actual value. Messages like `"Here's my full name and details"`, `"I'll share my details"`, `"Here is my information"` are explicitly forbidden — they imply the guest is about to hand over unique personal data. Suggestions must only be questions or service requests, never data submissions.

---

#### Result: New Suggestion Flow

```
Bot responds
  ↓
Layer 1 (_build_contextual_suggested_actions)
  → AWAITING_CONFIRMATION / ESCALATED / confirm pending_action: returns functional chips
  → Everything else: returns [] immediately
  ↓
Layer 2 (_run_next_action_suggestion_agent) runs — because Layer 1 returned []
  → Reads: assistant_response (primary anchor), history, service, phase, missing_fields
  → Writes: first-person guest messages grounded in what the bot just said
  ↓
Frontend uses data.suggested_actions directly (Layer 2 result)
  ↓
Only if Layer 2 also returned [] (rare edge case):
  → Frontend calls fallback endpoint with session_id + last_bot_message
  → Endpoint loads history, service, phase from session context
  → LLM generates grounded first-person suggestions from bot message + full context
```

---

## Update (March 16, 2026) — LLM Orchestrator as Single Authority: Disable Rule-Based Routing

### Background

A broken conversation was analyzed where:
- Room type queries were incorrectly answered by the complaint agent
- "yes" triggered a lost-and-found phase mismatch response
- Affirmative replies ("yes", "ok", "sure") were hijacked by keyword-based resume handler
- The LLM orchestrator's decisions were being overridden by rule-based layers beneath it

**Decision**: Disable all rule-based routing/response override layers. The LLM orchestrator is now the single authority on all routing and response decisions.

---

### Change 1 — Remove `_infer_plugin_ticket_fallback_intent` Call Sites
**File**: `services/chat_service.py`

**Problem**: `_infer_plugin_ticket_fallback_intent()` was a rule-based fallback that scanned message text for keywords and forced routing to the complaint/ticketing agent. This ran *after* the LLM orchestrator had already made its decision, silently overriding it. Room type questions would match complaint keywords and get rerouted to the wrong agent.

**Fix**: Removed both call sites. The LLM orchestrator's `action` field is now used directly without override.

```python
# REMOVED (was at ~line 6320):
# fallback_intent = self._infer_plugin_ticket_fallback_intent(user_message, context)
# if fallback_intent:
#     handler_result = await self._route_to_handler(fallback_intent, ...)

# REMOVED (was at ~line 7391):
# fallback_intent = self._infer_plugin_ticket_fallback_intent(user_message, context)
# if fallback_intent:
#     return await self._handle_fallback_ticket(...)

# Replaced with comment:
# Rule-based fallback intent inference removed — LLM orchestrator owns all routing decisions.
```

---

### Change 2 — Remove Phase Gate Early Return
**File**: `services/chat_service.py`

**Problem**: `_detect_ticketing_phase_service_mismatch()` ran before the LLM and checked if the current phase matched the current service. If a mismatch was detected, it fired an early return response bypassing the LLM entirely. The bug: the phase gate semantically matched "yes" against `lost_and_found` service during a `during_stay` phase check, and returned a phase mismatch response instead of letting the LLM handle the continuity of the pending flow.

**Fix**: Removed the `elif phase_gate_handler_result is not None:` early return block. Phase mismatches are now communicated to the LLM via the orchestrator payload (phase fields, service context), and the LLM decides what to say.

```python
# REMOVED:
# elif phase_gate_handler_result is not None:
#     handler_result = phase_gate_handler_result
#     response_text = handler_result.response_text
#     response_source = "ticketing_phase_gate"
```

---

### Change 3 — Replace Keyword Resume Handler with LLM Annotation
**File**: `services/llm_orchestration_service.py`

**Problem**: When `context.resume_prompt_sent` was True, a keyword-based handler checked for affirmatives ("yes", "ok", "sure", "yeah", "yep", "please", "alright", "go ahead") and negatives to decide whether to resume a suspended service. This was deterministic and wrong in ambiguous cases — any message containing those words would be intercepted.

**Fix**: Replaced with an annotation approach. The user's actual message is preserved but wrapped with a context annotation that tells the LLM what the situation is. The LLM reads the full annotation and decides whether to resume or abandon.

```python
# BEFORE (keyword-based):
# if context.resume_prompt_sent:
#     affirmatives = ["yes", "ok", "sure", ...]
#     if any(word in user_message.lower() for word in affirmatives):
#         return await self._resume_suspended_service(...)
#     else:
#         context.suspended_services.clear()

# AFTER (LLM annotation):
if context.resume_prompt_sent:
    context.resume_prompt_sent = False
    if context.suspended_services:
        suspended = context.suspended_services[0]
        svc_name = suspended.get("service_name", "previous request")
        user_message = (
            f"[CONTEXT: bot just asked whether to resume '{svc_name}'. "
            f"Guest replied: '{user_message}'. "
            f"Decide based on the reply whether to resume or abandon.]"
        )
```

---

### Change 4 — Add `pending_action_context` to Orchestrator Payload
**File**: `services/llm_orchestration_service.py`

**Problem**: The orchestrator received `pending_action` as an opaque string (e.g. `"confirm_room_booking"`). The LLM had no understanding of what this meant — what service it belonged to, what data had already been collected, or how far along the flow was. This caused the LLM to sometimes re-route away from the pending flow instead of continuing it.

**Fix**: Added `_build_pending_action_context()` helper method and `pending_action_context` field to the main orchestrator payload.

```python
def _build_pending_action_context(self, context: Any) -> str:
    """Return a human-readable sentence describing what the guest was mid-flow on."""
    pending_action = str(getattr(context, "pending_action", "") or "")
    if not pending_action:
        return ""
    # looks up service name from config, collects filled slot names
    # returns e.g.:
    # "Guest was mid-flow: confirm_room_booking (service: Room Booking, already collected: party_size)"
```

Payload field added:
```python
"pending_action_context": self._build_pending_action_context(context),
```

---

### Change 5 — Rewrite Main Orchestrator System Prompt
**File**: `services/llm_orchestration_service.py`

**Problem**: The old prompt had no explicit authority declaration, no short-message continuity rule, no ambiguity check step, and no explicit boundary between complaint and non-complaint intents. This caused the LLM to over-route to complaint handlers and fail to continue pending flows on short replies.

**Fix**: Full prompt rewrite with five explicit steps:

```
STEP 1: READ HISTORY
  → Always read history_last_10 then full_history_context first.
  → Use last_user_message + last_assistant_message as high-priority continuity anchors.
  → Resolve pronouns ('it', 'that', 'same', 'this') against last assistant message.

STEP 2: MID-FLOW CHECK
  → If pending_action is set: stay on that service, route back immediately.
  → Short replies ('yes', 'ok', 'sure', 'no', 'cancel', a number, a name, a date)
    when pending_action is set are ALWAYS a continuation — never re-route them.
  → Only interrupt if guest explicitly asks about a completely different topic in a full sentence.

STEP 2B: AMBIGUITY CHECK
  → If short/vague + no pending_action + last_assistant_message provides no context:
    do not guess, do not route. Set action=respond_only, ask a clarification question.

STEP 3: DECIDE WHAT THE GUEST NEEDS
  A) INFORMATION QUESTION → respond_only. Asking about room types/availability/prices/facilities is NEVER a complaint.
  B) SERVICE REQUEST → route to service. Wanting to book/order/request is NEVER a complaint.
  C) COMPLAINT / ISSUE → ONLY when guest explicitly reports a problem/malfunction/dissatisfaction.
  D) HUMAN / EMERGENCY → requires_human_handoff=true.

GROUNDING RULE
  → Use only provided policy + knowledge. Never invent prices, timings, availability, or capabilities.

AUTHORITY DECLARATION
  → "You are the single authority on routing and responding — no other layer will override your decision."
```

---

### Summary of Rule-Based Layers Disabled

| Layer | What it did | Status |
|---|---|---|
| `_infer_plugin_ticket_fallback_intent` (call site 1) | Keyword scan → force complaint routing | **Removed** |
| `_infer_plugin_ticket_fallback_intent` (call site 2) | Same, second call site | **Removed** |
| `_detect_ticketing_phase_service_mismatch` early return | Phase gate → early response before LLM | **Removed** |
| Keyword-based resume handler | Affirmative keywords → force service resume | **Replaced with LLM annotation** |

### New Flow

```
User message
  ↓
[If resume_prompt_sent: wrap message with LLM annotation]
  ↓
LLM Orchestrator — single authority
  Reads: history, pending_action_context, phase, service, KB
  Steps 1 → 2 → 2B → 3 → output JSON
  ↓
Handler executed based on LLM's action decision
  ↓
No rule-based override possible
```

---

## Update (March 16, 2026) — Additional Rule-Based Removal + Routing Fixes

### Bug 1 — `complaint_signal` Rule-Based Intent Override (chat_service.py)

**Problem**: A second rule-based complaint detection block was missed in the previous cleanup. It ran after the LLM orchestrator's decision and could silently override `effective_intent` to `IntentType.COMPLAINT`:

```python
complaint_signal = (
    self._looks_like_operational_issue_for_ticketing(message_lower)
    or self._looks_like_strong_ticket_issue_marker(message_lower)
)
if complaint_signal and effective_intent in {FAQ, MENU_REQUEST, UNCLEAR, GENERAL_SERVICE, ...}:
    effective_intent = IntentType.COMPLAINT   # overrode LLM
```

This caused the "complain agent" label to appear for room type queries and booking requests. The "complain agent" label is produced by `_service_llm_label_for_service()` whenever `effective_intent == IntentType.COMPLAINT`.

**Fix**: Removed the block entirely (same principle as previous cleanup).

```python
# Rule-based complaint signal detection removed — LLM orchestrator owns all intent decisions.
```

---

### Bug 2 — Hardcoded Fallback `"Please share one more detail so I can continue."` (chat_service.py)

**Problem**: When `response_text` was empty after orchestration (caused by confused service agent or wrong handler routing), a hardcoded string was returned. This masked real errors, gave a meaningless response, and broke the booking flow when the wrong agent's `pending_action` corrupted subsequent turns.

**Fix**: Replaced with a lightweight `llm_client.chat()` call that generates a contextual response using:
- Guest's actual last message
- `context.pending_action` (if mid-flow, asks for the next needed piece of info)
- A warning log so the root-cause empty-response is visible in logs

A minimal true hardcoded fallback (`"I'm here to help — could you let me know what you need?"`) remains only if the LLM call itself fails.

---

### Bug 3 — LLM Orchestrator Routing Info/Booking to Complaint Handler

**Problem**: The orchestrator prompt's Step 3C was not explicit enough about `ticketing_enabled`. All 4 services in the config have `ticketing_enabled=True`, so the LLM could interpret this as "all services are complaint-capable" and route bookings/info requests through `action=create_ticket`.

**Fix 1 — payload field `complaint_routing_note`**: Added to orchestrator payload:
```
"IMPORTANT: 'ticketing_enabled=true' means the service CAN raise a support ticket if the guest
reports a problem — it does NOT mean the service is a complaint handler. Room bookings, food orders,
and all other service requests are handled as normal service requests."
```

**Fix 2 — Strengthened Step 3C in system prompt**:
- Added explicit `action=create_ticket` negative examples: room type questions, booking requests, food orders, service inquiries
- Added "ROUTING SANITY CHECK" step: "is the guest asking for something or reporting a problem? When in doubt, treat as service request — never assume complaint."

---

### Root Cause Chain (for reference)

```
complaint_signal block fires (missed in prev cleanup)
  → effective_intent = COMPLAINT
  → _service_llm_label_for_service() returns "complain"
  → booking request → complaint handler → wrong pending_action set
  → subsequent turns: main orchestrator sees wrong pending_action
  → service agent returns empty response_text
  → hardcoded fallback fires: "Please share one more detail so I can continue."
  → guest is stuck in fallback loop
```

All links in this chain are now addressed.

---

## Update (March 16, 2026) — Suggestion Chips + Routing Label + Ticket Creation Fixes

### Bug 1 — "Ask another question" / "Talk to human" hardcoded chips

**Problem**: `_finalize_user_query_suggestions` at line 11065 had a final hardcoded fallback:
```python
return deduped or ["Ask another question", "Talk to human"]
```
When `decision.suggested_actions` was empty AND `_get_suggested_actions()` returned `[]` (as designed for all normal states), the cleaned list was empty and this fallback fired every time, showing generic meaningless chips.

**Fix**: Removed the hardcoded fallback — returns empty list instead, which triggers the frontend fallback endpoint to call the LLM suggestion agent with full context.

---

### Bug 2 — "complain agent" label for non-complaint service requests

**Two causes identified:**

**2a — LLM hallucinating complaint-flavored `target_service_id`**: The LLM orchestrator sometimes returned `target_service_id = "complaint"` or `"complaint_service"` even for room booking requests. `_service_llm_label_for_service` at line 2681 would directly return `"complain"` for these IDs regardless of intent.

**Fix**: Added guard — if `target_service_id` is in the complaint ID set but `effective_intent != COMPLAINT`, return `fallback` instead of `"complain"`. Added pre-dispatch sanitization that clears `decision.target_service_id` when it's complaint-flavored for a non-complaint `effective_intent`.

**2b — Orchestrator prompt not explicit enough about `target_service_id` values**: Prompt said "use exact service id" but didn't say which IDs are forbidden.

**Fix**: Added explicit instruction to orchestrator prompt Step 3B: "NEVER set target_service_id to 'complaint', 'complaint_service', 'complain', or any complaint-related value for a service request."

---

### Bug 3 — Ticket not created on booking confirmation

**Problem**: The main orchestrator was handling all slot collection in `respond_only` mode (collecting name, phone, dates, party size). When user said "yes confirm", the orchestrator returned `action=respond_only` with a confirmation text — but no `action=create_ticket`, so the ticket creation code path was never triggered. Guest got a confirmation message with no actual booking reference.

**Fix**: Added new **Step 3C: BOOKING CONFIRMATION** to the orchestrator prompt:
- When `pending_action` contains "confirm" AND guest confirms → set `action=create_ticket`, `ticket.required=true`, `ticket.ready_to_create=true`, `ticket.category="request"`
- Read all collected fields from `pending_data_public` and populate `ticket.issue` with a summary
- If guest declines → set `action=cancel_pending`

---

### Bug 4 — Orchestrator collecting slots in `respond_only` instead of dispatching to service agent

**Problem**: For service requests like "I need a room", the orchestrator was returning `action=respond_only` and doing the slot collection itself (asking for name, phone, dates, etc.) rather than dispatching to the room booking service agent which has the proper slot schema and ticket creation logic.

**Fix**: Strengthened Step 3B in orchestrator prompt:
- "ALWAYS set action=dispatch_handler with the exact target_service_id"
- "Do NOT collect slots yourself via respond_only — the service agent handles all slot collection"

---

### Changes Made

| File | Change |
|---|---|
| `chat_service.py:11065` | Removed `or ["Ask another question", "Talk to human"]` fallback → returns `deduped` (empty if nothing) |
| `chat_service.py:2681` | Guard: complaint service ID + non-complaint intent → return `fallback` instead of `"complain"` |
| `chat_service.py:3109` | Pre-dispatch sanitization: clears complaint-flavored `target_service_id` for non-complaint requests |
| `llm_orchestration_service.py` | Step 3B: explicit forbidden IDs + always dispatch_handler for service requests |
| `llm_orchestration_service.py` | New Step 3C: booking confirmation → `create_ticket` with pending_data fields |

---

## Update (March 16, 2026) — Remove Rule-Based Blocks from Handler Dispatch + Prompt Improvements

### Changes Made

#### Prompt: Orchestrator Step 2 — In-topic questions stay on service agent

**Problem**: "will I be picked up in a limo?" during airport transfer flow → LLM treated it as a full-sentence interrupt → returned `respond_only` → main orchestrator answered instead of airport transfer agent.

**Fix**: Added to Step 2: *"A question about the current service (asking about vehicle type during airport transfer, room details during room booking, etc.) is NOT an interrupt — dispatch to the same service agent so it can answer and continue collecting slots. Only interrupt if guest explicitly asks to start a completely unrelated and different service."*

---

#### Prompt: Orchestrator Step 3B — Always dispatch_handler from first message

**Problem**: LLM sometimes answered service requests directly (`respond_only`) instead of dispatching immediately to the service agent on the first message.

**Fix**: Step 3B now says: *"From the very first message requesting a service — even just 'I need a room' — IMMEDIATELY set action=dispatch_handler. Do not answer first, do not ask for details yourself. The service agent handles ALL slot collection."*

---

#### Code removal: `_resolve_unified_complaint_routing_intent` gutted of all rule-based paths

**Problem**: `_dispatch_to_handler` called `_resolve_unified_complaint_routing_intent` before routing. This function had three rule-based complaint overrides that ran after the LLM orchestrator had already made its decision:

1. **`_is_ticketing_pending_action`** (line 7398): If `pending_action` was any ticketing-related action name, the next message was force-routed to complaint handler regardless of what the guest said or what the orchestrator decided.

2. **`_should_route_full_kb_ticketing_handler`** (line 7403): Rule-based gate that decided whether to attempt complaint routing at all — based on intent, pending_action, and message content. Pure keyword/state logic.

3. **`_llm_response_implies_staff_action`** (line 7416): Scanned the LLM's own response text for phrases like "our team", "staff", "we will arrange" and if found, re-routed to complaint handler. This was literally using the LLM's output to override the LLM's own decision.

**Fix**: Removed all three. `_resolve_unified_complaint_routing_intent` now only routes to complaint handler when `effective_intent == IntentType.COMPLAINT` — i.e., when the LLM orchestrator explicitly decided it was a complaint.

```python
# Now:
if effective_intent == IntentType.COMPLAINT:
    return IntentType.COMPLAINT, "complaint_intent", "intent"
return None, "", ""
```

**Why this is correct**: The LLM orchestrator reads history, pending_action_context, service context, and KB before deciding intent. It is the right authority. Scanning pending_action names and response text to second-guess it is fundamentally wrong and caused complaint handler to fire for normal booking confirmations.

---

### Rule-Based Layers Removed (cumulative total this session)

| Layer | File | Status |
|---|---|---|
| `_infer_plugin_ticket_fallback_intent` (×2) | `chat_service.py` | Removed |
| Phase gate early return | `chat_service.py` | Removed |
| Keyword resume handler | `llm_orchestration_service.py` | Replaced with LLM annotation |
| `complaint_signal` intent override | `chat_service.py` | Removed |
| `_is_ticketing_pending_action` → COMPLAINT route | `chat_service.py` | Removed |
| `_should_route_full_kb_ticketing_handler` path | `chat_service.py` | Removed |
| `_llm_response_implies_staff_action` → COMPLAINT route | `chat_service.py` | Removed |
