# KePSLA Conversational AI - LLM & AI Design

**Document Version**: 1.0
**Created**: February 3, 2026
**Status**: Design Phase

---

## Table of Contents

1. [LLM Strategy Overview](#1-llm-strategy-overview)
2. [Model Selection](#2-model-selection)
3. [Prompt Engineering](#3-prompt-engineering)
4. [Structured Output Schemas](#4-structured-output-schemas)
5. [Confidence Scoring System](#5-confidence-scoring-system)
6. [RAG Pipeline Design](#6-rag-pipeline-design)
7. [Few-Shot Examples](#7-few-shot-examples)
8. [Error Handling](#8-error-handling)
9. [Cost Optimization](#9-cost-optimization)
10. [Testing & Evaluation](#10-testing--evaluation)

---

## 1. LLM Strategy Overview

### 1.1 Design Goals

| Goal | Strategy |
|------|----------|
| **Accuracy** | Use best model (GPT-4.1) for complex reasoning |
| **Speed** | Use fast model (GPT-4.1-mini) for simple tasks |
| **Cost** | Route 90% of requests to cheaper models |
| **Reliability** | Fallback chain, retry logic, caching |
| **Consistency** | Structured outputs, validation layer |

### 1.2 LLM Call Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                              LLM CALL ARCHITECTURE                                   │
└─────────────────────────────────────────────────────────────────────────────────────┘

User Message
      │
      ▼
┌─────────────────┐     ┌─────────────────────────────────────────┐
│  1. CACHE CHECK │────▶│ Check if similar query cached           │
└────────┬────────┘     │ Cache hit rate target: 30%              │
         │              └─────────────────────────────────────────┘
         │ Cache miss
         ▼
┌─────────────────┐     ┌─────────────────────────────────────────┐
│  2. MODEL ROUTER│────▶│ Route to appropriate model based on:    │
└────────┬────────┘     │ • Task complexity                       │
         │              │ • Required accuracy                     │
         │              │ • Cost constraints                      │
         │              └─────────────────────────────────────────┘
         │
    ┌────┴────┬─────────────────┐
    │         │                 │
    ▼         ▼                 ▼
┌───────┐ ┌───────┐       ┌───────┐
│ FAST  │ │STANDARD│      │PREMIUM│
│GPT-4.1│ │GPT-4.1 │      │GPT-4.1│
│-mini  │ │-mini   │      │       │
│       │ │        │      │       │
│Simple │ │Intent  │      │Complex│
│FAQ    │ │Classify│      │Reason │
│Confirm│ │Response│      │Edge   │
│       │ │        │      │Cases  │
└───┬───┘ └───┬────┘      └───┬───┘
    │         │               │
    └─────────┼───────────────┘
              │
              ▼
┌─────────────────┐
│  3. PARSE OUTPUT│  Structured output → Pydantic models
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  4. VALIDATE    │  Check against capabilities, history
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  5. CACHE RESULT│  Store for future similar queries
└─────────────────┘
```

---

## 2. Model Selection

### 2.1 Model Routing Matrix

| Task | Model | Cost/1K tokens | Latency | Why |
|------|-------|----------------|---------|-----|
| Intent Classification | gpt-4.1-mini | $0.002 | ~300ms | Fast, good enough |
| Entity Extraction | gpt-4.1-mini | $0.002 | ~300ms | Structured output |
| Response Generation | gpt-4.1-mini | $0.002 | ~500ms | Most common |
| Response Validation | gpt-4.1-mini | $0.002 | ~200ms | Simple yes/no |
| Complex Reasoning | gpt-4.1 | $0.03 | ~800ms | Edge cases only |
| Embeddings | text-embedding-3-small | $0.00002 | ~100ms | Cost-effective |
| Review Analysis | gpt-5-nano | $0.001 | ~200ms | Batch processing |

### 2.2 Model Fallback Chain

```python
MODEL_FALLBACK_CHAIN = [
    {
        "model": "gpt-4.1-mini",
        "max_retries": 2,
        "timeout": 10,
        "conditions": ["default"]
    },
    {
        "model": "gpt-4.1",
        "max_retries": 2,
        "timeout": 20,
        "conditions": ["low_confidence", "complex_intent", "validation_failed"]
    },
    {
        "model": "gpt-4.1-mini",  # Different region/endpoint
        "max_retries": 1,
        "timeout": 15,
        "conditions": ["rate_limited", "timeout"]
    }
]
```

### 2.3 Model Upgrade Triggers

```python
UPGRADE_TO_GPT4_CONDITIONS = [
    # Confidence-based
    {"condition": "confidence < 0.7", "reason": "low_confidence"},

    # Complexity-based
    {"condition": "entity_count > 5", "reason": "complex_request"},
    {"condition": "ambiguous_intent == True", "reason": "ambiguity"},

    # Validation-based
    {"condition": "validation_failed == True", "reason": "needs_better_reasoning"},

    # Content-based
    {"condition": "contains_negation == True", "reason": "tricky_logic"},
    {"condition": "multi_part_request == True", "reason": "complex_request"},
]
```

---

## 3. Prompt Engineering

### 3.1 Prompt Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                              PROMPT STRUCTURE                                        │
└─────────────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────────────┐
│  SYSTEM PROMPT (Cached)                                                             │
│                                                                                     │
│  ┌─────────────────────────────────────────────────────────────────────────────┐   │
│  │  BASE IDENTITY                                                               │   │
│  │  "You are {bot_name}, the AI concierge for {hotel_name}..."                 │   │
│  └─────────────────────────────────────────────────────────────────────────────┘   │
│                                                                                     │
│  ┌─────────────────────────────────────────────────────────────────────────────┐   │
│  │  CAPABILITIES (from registry)                                                │   │
│  │  "You CAN: room_service (IRD only), table_booking, airport_transfer..."     │   │
│  │  "You CANNOT: deliver from Kadak/Aviator, intercity cabs..."                │   │
│  └─────────────────────────────────────────────────────────────────────────────┘   │
│                                                                                     │
│  ┌─────────────────────────────────────────────────────────────────────────────┐   │
│  │  RULES & CONSTRAINTS                                                         │   │
│  │  "Always verify before promising. Never contradict yourself..."              │   │
│  └─────────────────────────────────────────────────────────────────────────────┘   │
│                                                                                     │
│  ┌─────────────────────────────────────────────────────────────────────────────┐   │
│  │  OUTPUT FORMAT                                                               │   │
│  │  "Respond in JSON format matching the schema..."                             │   │
│  └─────────────────────────────────────────────────────────────────────────────┘   │
│                                                                                     │
└─────────────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────────────┐
│  USER CONTEXT (Dynamic)                                                             │
│                                                                                     │
│  ┌─────────────────────────────────────────────────────────────────────────────┐   │
│  │  GUEST INFO                                                                  │   │
│  │  "Guest: John Doe, Room 301, Check-in: Feb 1, Check-out: Feb 5"             │   │
│  └─────────────────────────────────────────────────────────────────────────────┘   │
│                                                                                     │
│  ┌─────────────────────────────────────────────────────────────────────────────┐   │
│  │  CONVERSATION HISTORY                                                        │   │
│  │  [Last 10 messages + summary if longer]                                      │   │
│  └─────────────────────────────────────────────────────────────────────────────┘   │
│                                                                                     │
│  ┌─────────────────────────────────────────────────────────────────────────────┐   │
│  │  OPEN TICKETS                                                                │   │
│  │  "Open ticket #5090: Table reservation at Aviator (pending details)"         │   │
│  └─────────────────────────────────────────────────────────────────────────────┘   │
│                                                                                     │
│  ┌─────────────────────────────────────────────────────────────────────────────┐   │
│  │  CURRENT STATE                                                               │   │
│  │  "State: COLLECTING, Intent: ORDER, Entities needed: [item_type]"           │   │
│  └─────────────────────────────────────────────────────────────────────────────┘   │
│                                                                                     │
└─────────────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────────────┐
│  CURRENT MESSAGE                                                                    │
│  "User: I want burger in room"                                                      │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

### 3.2 Base System Prompt Template

```python
BASE_SYSTEM_PROMPT = """
You are {bot_name}, the AI concierge for {hotel_name}.
Your role is to assist hotel guests with their requests during their stay.

## YOUR IDENTITY
- Name: {bot_name}
- Hotel: {hotel_name}
- Location: {hotel_location}
- Personality: Helpful, professional, warm but not overly casual

## CAPABILITIES - WHAT YOU CAN DO
{capabilities_list}

## LIMITATIONS - WHAT YOU CANNOT DO
{limitations_list}

CRITICAL: Before making any promise or commitment, verify it's in your capabilities.
If unsure, say "Let me check with our team" and create a ticket.

## RULES
1. NEVER contradict yourself. If you said something earlier, be consistent.
2. NEVER promise something outside your capabilities.
3. ALWAYS maintain context from the conversation.
4. If the guest says "yes" after you asked for confirmation, EXECUTE the action.
5. If you don't understand, ask for clarification - don't guess.
6. If confidence is low, escalate to human support.

## GUEST CONTEXT
{guest_context}

## CONVERSATION STATE
Current State: {current_state}
Pending Confirmation: {pending_confirmation}
Entities Collected: {entities_collected}

## OUTPUT FORMAT
{output_format_instructions}
"""
```

### 3.3 Intent Classification Prompt

```python
INTENT_CLASSIFICATION_PROMPT = """
Classify the user's intent from the message below.

## AVAILABLE INTENTS
- ORDER: Guest wants to order food/drinks for delivery to room
- MENU: Guest wants to see a menu (one or more)
- BOOKING: Guest wants to book a table at restaurant
- COMPLAINT: Guest has a complaint or issue
- REQUEST: General service request (towels, housekeeping, etc.)
- FAQ: Guest asking a question (what time, where is, do you have)
- ESCALATION: Guest wants to talk to human / manager
- GREETING: Hello, hi, good morning
- CONFIRMATION: Yes, ok, sure, confirm, proceed
- CANCELLATION: No, cancel, nevermind, don't want
- FOLLOW_UP: Additional details for previous intent
- OTHER: Doesn't fit above categories

## IMPORTANT RULES
1. If message is "yes", "ok", "sure", "proceed" → CONFIRMATION
2. If message is "no", "cancel", "nevermind" → CANCELLATION
3. If message adds details to previous request → FOLLOW_UP
4. If asking for multiple menus → MENU (extract count)

## CONTEXT
Previous Intent: {previous_intent}
Pending Confirmation: {pending_confirmation}
Conversation History: {recent_messages}

## MESSAGE TO CLASSIFY
"{message}"

Respond with JSON:
{{
    "intent": "<intent_type>",
    "confidence": <0.0-1.0>,
    "entities": {{...extracted entities...}},
    "is_multi_intent": <true/false>,
    "secondary_intent": "<if multi-intent>",
    "reasoning": "<brief explanation>"
}}
"""
```

### 3.4 Response Generation Prompt

```python
RESPONSE_GENERATION_PROMPT = """
Generate a response for the guest based on the context below.

## GUEST
Name: {guest_name}
Room: {room_number}

## INTENT
Type: {intent_type}
Entities: {entities}
Handler Result: {handler_result}

## CAPABILITIES CHECK
{capability_check_results}

## RULES FOR RESPONSE
1. Be warm but professional
2. Be concise - no unnecessary words
3. If handler succeeded, confirm what was done
4. If handler needs more info, ask specific questions
5. If something can't be done, explain why and offer alternatives
6. NEVER say "I'll check" if you actually can't check
7. Use guest's name occasionally (not every message)

## DO NOT
- Promise things outside capabilities
- Give vague responses like "I'll see what I can do"
- Repeat information the guest already knows
- Be overly apologetic

## GENERATE RESPONSE
"""
```

### 3.5 Response Validation Prompt

```python
VALIDATION_PROMPT = """
Check if this response is valid and consistent.

## RESPONSE TO VALIDATE
"{response}"

## CAPABILITIES
{capabilities}

## CONVERSATION HISTORY
{history}

## CHECKS TO PERFORM
1. Does response promise anything outside capabilities? (e.g., delivery from dine-in restaurant)
2. Does response contradict anything said earlier in conversation?
3. Does response contain accurate information?
4. Is the response appropriate for the guest's intent?

## RESPOND WITH JSON
{{
    "valid": <true/false>,
    "issues": [
        {{
            "type": "capability_violation" | "contradiction" | "inaccuracy" | "inappropriate",
            "description": "<what's wrong>",
            "severity": "high" | "medium" | "low"
        }}
    ],
    "suggested_fix": "<how to fix if invalid>"
}}
"""
```

---

## 4. Structured Output Schemas

### 4.1 Core Pydantic Models

```python
from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime
from enum import Enum


class IntentType(str, Enum):
    ORDER = "order"
    MENU = "menu"
    BOOKING = "booking"
    COMPLAINT = "complaint"
    REQUEST = "request"
    FAQ = "faq"
    ESCALATION = "escalation"
    GREETING = "greeting"
    CONFIRMATION = "confirmation"
    CANCELLATION = "cancellation"
    FOLLOW_UP = "follow_up"
    OTHER = "other"


class ClassifiedIntent(BaseModel):
    """Output from intent classification LLM call"""

    intent_type: IntentType
    confidence: float = Field(ge=0.0, le=1.0)
    entities: dict = Field(default_factory=dict)
    is_multi_intent: bool = False
    secondary_intent: Optional[IntentType] = None
    reasoning: str = ""

    class Config:
        json_schema_extra = {
            "example": {
                "intent_type": "order",
                "confidence": 0.92,
                "entities": {
                    "item": "burger",
                    "location": "room",
                    "quantity": 1
                },
                "is_multi_intent": False,
                "reasoning": "User explicitly said 'I want burger in room'"
            }
        }


class MenuRequest(BaseModel):
    """Structured menu request"""

    menu_names: list[str] = Field(default_factory=list)
    menu_count: int = 1
    purpose: Optional[str] = None  # "ordering", "browsing", "comparing"


class OrderRequest(BaseModel):
    """Structured order request"""

    items: list[dict] = Field(default_factory=list)
    delivery_location: str = "room"
    special_instructions: Optional[str] = None
    urgency: Literal["normal", "urgent"] = "normal"


class BookingRequest(BaseModel):
    """Structured booking request"""

    restaurant: str
    party_size: Optional[int] = None
    date: Optional[str] = None
    time: Optional[str] = None
    special_requests: Optional[str] = None


class TicketRequest(BaseModel):
    """Structured ticket creation request"""

    type: Literal["complaint", "request", "booking", "lead"]
    category: str
    subcategory: Optional[str] = None
    description: str
    priority: Literal["low", "medium", "high", "urgent"] = "medium"
    department: Optional[str] = None


class GeneratedResponse(BaseModel):
    """Output from response generation LLM call"""

    text: str
    media: Optional[list[dict]] = None  # [{type, url, caption}]
    quick_replies: Optional[list[str]] = None
    requires_confirmation: bool = False
    confirmation_summary: Optional[str] = None
    ticket_to_create: Optional[TicketRequest] = None
    escalate: bool = False
    escalation_reason: Optional[str] = None


class ValidationResult(BaseModel):
    """Output from validation LLM call"""

    valid: bool
    issues: list[dict] = Field(default_factory=list)
    suggested_fix: Optional[str] = None


class ConversationState(BaseModel):
    """Full conversation state stored in Redis"""

    session_id: str
    current_state: str = "IDLE"
    previous_state: Optional[str] = None

    # Intent tracking
    current_intent: Optional[IntentType] = None
    intent_confidence: float = 0.0

    # Entity collection
    entities_collected: dict = Field(default_factory=dict)
    entities_required: list[str] = Field(default_factory=list)

    # Confirmation tracking
    pending_confirmation: Optional[dict] = None
    confirmation_expires_at: Optional[datetime] = None

    # Conversation metadata
    turns: int = 0
    last_activity: datetime = Field(default_factory=datetime.utcnow)

    # LLM tracking
    total_tokens: int = 0
    total_cost: float = 0.0
```

### 4.2 OpenAI Structured Output Configuration

```python
from openai import OpenAI

client = OpenAI()

def classify_intent(message: str, context: dict) -> ClassifiedIntent:
    """
    Use OpenAI's structured output feature for reliable parsing
    """
    response = client.beta.chat.completions.parse(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": INTENT_CLASSIFICATION_PROMPT.format(**context)},
            {"role": "user", "content": message}
        ],
        response_format=ClassifiedIntent,  # Pydantic model
    )

    return response.choices[0].message.parsed


def generate_response(intent: ClassifiedIntent, context: dict) -> GeneratedResponse:
    """
    Generate structured response
    """
    response = client.beta.chat.completions.parse(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": RESPONSE_GENERATION_PROMPT.format(**context)},
            {"role": "user", "content": f"Intent: {intent.model_dump_json()}"}
        ],
        response_format=GeneratedResponse,
    )

    return response.choices[0].message.parsed
```

---

## 5. Confidence Scoring System

### 5.1 Confidence Calculation

```python
class ConfidenceScorer:
    """
    Multi-factor confidence scoring system.
    Solves Lumira's lack of confidence awareness.
    """

    def calculate(
        self,
        llm_confidence: float,
        context: UnifiedContext,
        intent: ClassifiedIntent
    ) -> ConfidenceScore:

        factors = {
            # LLM's own confidence (weight: 40%)
            "llm_confidence": llm_confidence * 0.4,

            # Intent clarity (weight: 20%)
            "intent_clarity": self._score_intent_clarity(intent) * 0.2,

            # Entity completeness (weight: 15%)
            "entity_completeness": self._score_entity_completeness(intent) * 0.15,

            # Context relevance (weight: 15%)
            "context_relevance": self._score_context_relevance(intent, context) * 0.15,

            # Historical accuracy (weight: 10%)
            "historical_accuracy": self._get_historical_accuracy(intent.intent_type) * 0.1,
        }

        total_score = sum(factors.values())

        return ConfidenceScore(
            total=total_score,
            factors=factors,
            action=self._determine_action(total_score)
        )

    def _score_intent_clarity(self, intent: ClassifiedIntent) -> float:
        """Score based on how clear the intent is"""
        score = 1.0

        # Penalize multi-intent
        if intent.is_multi_intent:
            score -= 0.2

        # Penalize OTHER intent
        if intent.intent_type == IntentType.OTHER:
            score -= 0.5

        # Penalize short reasoning
        if len(intent.reasoning) < 20:
            score -= 0.1

        return max(0.0, score)

    def _score_entity_completeness(self, intent: ClassifiedIntent) -> float:
        """Score based on entity extraction quality"""
        required = REQUIRED_ENTITIES.get(intent.intent_type, [])

        if not required:
            return 1.0

        extracted = set(intent.entities.keys())
        missing = set(required) - extracted

        return 1.0 - (len(missing) / len(required))

    def _determine_action(self, score: float) -> str:
        """Determine action based on confidence score"""
        if score >= 0.8:
            return "PROCEED"
        elif score >= 0.6:
            return "PROCEED_WITH_CAUTION"
        elif score >= 0.4:
            return "CLARIFY"
        else:
            return "ESCALATE"


# Confidence thresholds
CONFIDENCE_THRESHOLDS = {
    "PROCEED": 0.8,           # Confident, execute action
    "PROCEED_WITH_CAUTION": 0.6,  # Proceed but validate response
    "CLARIFY": 0.4,           # Ask clarifying questions
    "ESCALATE": 0.0,          # Hand off to human
}
```

### 5.2 Confidence-Based Routing

```python
async def process_with_confidence(
    message: str,
    context: UnifiedContext,
    state: ConversationState
) -> ProcessingResult:

    # Step 1: Classify intent
    intent = await classify_intent(message, context)

    # Step 2: Calculate confidence
    confidence = confidence_scorer.calculate(
        llm_confidence=intent.confidence,
        context=context,
        intent=intent
    )

    # Step 3: Route based on confidence
    if confidence.action == "ESCALATE":
        return await escalation_handler.handle(
            reason=f"Low confidence: {confidence.total:.2f}",
            context=context
        )

    if confidence.action == "CLARIFY":
        return await generate_clarification(intent, context)

    if confidence.action == "PROCEED_WITH_CAUTION":
        # Use better model for response
        response = await generate_response(
            intent, context,
            model="gpt-4.1"  # Upgrade to premium model
        )
    else:
        response = await generate_response(intent, context)

    # Step 4: Validate response
    validation = await validate_response(response, context)

    if not validation.valid:
        if confidence.total >= 0.6:
            # Regenerate with corrections
            response = await regenerate_with_fix(
                response, validation.suggested_fix, context
            )
        else:
            # Escalate
            return await escalation_handler.handle(
                reason=f"Validation failed: {validation.issues}",
                context=context
            )

    return ProcessingResult(
        response=response,
        confidence=confidence,
        intent=intent
    )
```

### 5.3 Escalation Triggers

```python
ESCALATION_TRIGGERS = [
    # Confidence-based
    {
        "condition": lambda c: c.total < 0.4,
        "reason": "Confidence too low",
        "mode": "ticket"
    },

    # Explicit request
    {
        "condition": lambda c, i: i.intent_type == IntentType.ESCALATION,
        "reason": "Guest requested human support",
        "mode": "live_chat"
    },

    # Repeated clarification
    {
        "condition": lambda c, s: s.turns_in_state > 3 and s.current_state == "COLLECTING",
        "reason": "Multiple clarification attempts",
        "mode": "live_chat"
    },

    # Frustration detection
    {
        "condition": lambda c, m: detect_frustration(m),
        "reason": "Guest appears frustrated",
        "mode": "live_chat"
    },

    # Validation failure
    {
        "condition": lambda v: v and not v.valid and any(i["severity"] == "high" for i in v.issues),
        "reason": "Cannot provide accurate response",
        "mode": "ticket"
    },

    # Capability limitation
    {
        "condition": lambda cap: cap and not cap.allowed,
        "reason": f"Cannot fulfill request: {cap.reason if cap else 'unknown'}",
        "mode": "ticket"
    },
]
```

---

## 6. RAG Pipeline Design

### 6.1 RAG Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                              RAG PIPELINE                                            │
└─────────────────────────────────────────────────────────────────────────────────────┘

User Query: "What time does the pool close?"
                │
                ▼
┌─────────────────────┐
│  1. QUERY ANALYSIS  │
│                     │
│  Is this RAG-able?  │───▶ Yes (FAQ-type question)
│  Extract keywords   │───▶ ["pool", "time", "close", "hours"]
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│  2. QUERY REWRITE   │
│                     │
│  Expand query for   │───▶ "pool closing time hours operation schedule"
│  better retrieval   │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│  3. EMBEDDING       │
│                     │
│  text-embedding-3   │───▶ [0.023, -0.041, 0.089, ...]
│  -small             │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐     ┌─────────────────────────────────────────┐
│  4. RETRIEVAL       │     │  FAISS Index (per hotel)                │
│                     │◄───▶│  - Hotel amenities                      │
│  Top-K similar      │     │  - Restaurant menus                     │
│  chunks (K=8)       │     │  - Policies                             │
│                     │     │  - FAQs                                 │
└──────────┬──────────┘     └─────────────────────────────────────────┘
           │
           ▼
┌─────────────────────┐     ┌─────────────────────────────────────────┐
│  5. RERANKING       │     │  Retrieved Chunks:                      │
│                     │     │  1. "Pool hours: 6 AM - 10 PM" (0.92)   │
│  LLM-based rerank   │────▶│  2. "Gym hours: 24 hours" (0.45)        │
│  by relevance       │     │  3. "Spa hours: 10 AM - 8 PM" (0.41)    │
│                     │     │                                         │
│  Keep top 3         │     │  After rerank: Keep #1 only             │
└──────────┬──────────┘     └─────────────────────────────────────────┘
           │
           ▼
┌─────────────────────┐
│  6. GENERATION      │
│                     │
│  Generate response  │───▶ "The pool is open from 6 AM to 10 PM daily."
│  using context      │
└─────────────────────┘
```

### 6.2 RAG Implementation

```python
class RAGPipeline:
    def __init__(self, hotel_id: str):
        self.embedder = OpenAIEmbeddings(model="text-embedding-3-small")
        self.index = self._load_or_create_index(hotel_id)
        self.reranker = LLMReranker()

    async def query(self, question: str, context: UnifiedContext) -> RAGResult:
        # Step 1: Check if RAG is appropriate
        if not self._should_use_rag(question):
            return RAGResult(used=False)

        # Step 2: Rewrite query for better retrieval
        rewritten = await self._rewrite_query(question, context)

        # Step 3: Embed query
        query_embedding = await self.embedder.embed(rewritten)

        # Step 4: Retrieve similar chunks
        chunks = self.index.similarity_search(
            query_embedding,
            k=8,
            filter={"hotel_id": context.hotel_id}
        )

        # Step 5: Rerank by relevance
        reranked = await self.reranker.rerank(
            query=question,
            chunks=chunks,
            top_k=3
        )

        # Step 6: Format context
        rag_context = self._format_chunks(reranked)

        return RAGResult(
            used=True,
            context=rag_context,
            chunks=reranked,
            confidence=reranked[0].score if reranked else 0.0
        )

    async def _rewrite_query(self, query: str, context: UnifiedContext) -> str:
        """Expand query for better retrieval"""
        prompt = f"""
        Rewrite this query to improve search retrieval.
        Add synonyms and related terms.
        Keep it concise (under 50 words).

        Original: {query}
        Hotel: {context.hotel_name}

        Rewritten query:
        """

        response = await llm_client.generate(prompt, model="gpt-4.1-mini")
        return response.strip()

    def _should_use_rag(self, question: str) -> bool:
        """Determine if RAG is appropriate for this question"""
        rag_indicators = [
            "what time", "when", "where", "how much", "do you have",
            "is there", "can i", "what are", "tell me about",
            "hours", "price", "location", "menu", "policy"
        ]
        return any(ind in question.lower() for ind in rag_indicators)
```

### 6.3 Index Management

```python
class IndexManager:
    """Manage FAISS indexes per hotel"""

    def __init__(self, storage_path: str):
        self.storage_path = storage_path

    async def create_index(self, hotel_id: str, documents: list[Document]) -> None:
        """Create or update hotel index"""

        # Chunk documents
        chunks = self._chunk_documents(documents)

        # Generate embeddings
        embeddings = await self._generate_embeddings(chunks)

        # Create FAISS index
        index = faiss.IndexFlatIP(1536)  # Inner product for cosine sim
        index.add(embeddings)

        # Save index and metadata
        faiss.write_index(index, f"{self.storage_path}/{hotel_id}.index")
        self._save_metadata(hotel_id, chunks)

    def _chunk_documents(self, documents: list[Document]) -> list[Chunk]:
        """Split documents into chunks"""
        chunker = RecursiveCharacterTextSplitter(
            chunk_size=500,
            chunk_overlap=50,
            separators=["\n\n", "\n", ". ", " "]
        )

        chunks = []
        for doc in documents:
            doc_chunks = chunker.split_text(doc.content)
            for i, chunk_text in enumerate(doc_chunks):
                chunks.append(Chunk(
                    text=chunk_text,
                    source=doc.source,
                    metadata={
                        "document_id": doc.id,
                        "chunk_index": i,
                        "category": doc.category
                    }
                ))

        return chunks
```

---

## 7. Few-Shot Examples

### 7.1 Intent Classification Examples

```python
INTENT_CLASSIFICATION_EXAMPLES = [
    # Confirmation handling (CRITICAL - Lumira failure)
    {
        "context": {
            "previous_intent": "ORDER",
            "pending_confirmation": "Burger order for Room 301"
        },
        "message": "yes",
        "expected": {
            "intent_type": "confirmation",
            "confidence": 0.95,
            "entities": {},
            "reasoning": "User said 'yes' after order confirmation prompt"
        }
    },
    {
        "context": {
            "previous_intent": "BOOKING",
            "pending_confirmation": "Table for 5 at Aviator 7:30 PM"
        },
        "message": "ok sounds good",
        "expected": {
            "intent_type": "confirmation",
            "confidence": 0.93,
            "entities": {},
            "reasoning": "Affirmative response to booking confirmation"
        }
    },

    # Multiple menus (Lumira failure)
    {
        "context": {},
        "message": "I want all 3 menus - kadak, aviator, and IRD",
        "expected": {
            "intent_type": "menu",
            "confidence": 0.95,
            "entities": {
                "menus": ["kadak", "aviator", "IRD"],
                "count": 3
            },
            "reasoning": "Explicit request for multiple menus"
        }
    },
    {
        "context": {},
        "message": "give me all three menus",
        "expected": {
            "intent_type": "menu",
            "confidence": 0.90,
            "entities": {
                "count": 3,
                "menus": "all"
            },
            "reasoning": "Request for all menus without naming them"
        }
    },

    # Context continuation (Lumira failure - burger in room)
    {
        "context": {
            "previous_intent": "ORDER",
            "entities_collected": {"item": "burger"}
        },
        "message": "room",
        "expected": {
            "intent_type": "follow_up",
            "confidence": 0.88,
            "entities": {
                "location": "room"
            },
            "reasoning": "Single word 'room' in context of food order = delivery location"
        }
    },

    # Escalation requests
    {
        "context": {},
        "message": "I want to speak to a manager",
        "expected": {
            "intent_type": "escalation",
            "confidence": 0.98,
            "entities": {
                "target": "manager"
            },
            "reasoning": "Explicit request for human manager"
        }
    },
    {
        "context": {},
        "message": "this is ridiculous get me a human",
        "expected": {
            "intent_type": "escalation",
            "confidence": 0.95,
            "entities": {
                "frustration": True
            },
            "reasoning": "Frustrated request for human support"
        }
    },
]
```

### 7.2 Response Generation Examples

```python
RESPONSE_GENERATION_EXAMPLES = [
    # Correct capability handling (Lumira failure - Kadak delivery)
    {
        "intent": {"type": "ORDER", "entities": {"restaurant": "Kadak", "location": "room"}},
        "capability_check": {"allowed": False, "reason": "Kadak is dine-in only"},
        "correct_response": "I'd love to help! However, Kadak is a dine-in restaurant and doesn't offer room delivery. I can help you with:\n1. Book a table at Kadak\n2. Order from our In-Room Dining menu which has great options\n\nWhat would you prefer?",
        "incorrect_response": "Sure, I'll arrange delivery from Kadak to your room!",
        "why": "Must check capability BEFORE promising"
    },

    # Multiple menus (Lumira failure)
    {
        "intent": {"type": "MENU", "entities": {"menus": ["kadak", "aviator", "IRD"], "count": 3}},
        "correct_response": "[Sends all 3 menu PDFs]\n\nHere are the menus you requested:\n1. Kadak Menu\n2. Aviator Menu\n3. In-Room Dining Menu\n\nLet me know when you're ready to order!",
        "incorrect_response": "I can only send one menu at a time. Which one would you like?",
        "why": "System should support multiple menus"
    },

    # Confirmation handling (Lumira failure)
    {
        "pending_confirmation": {"action": "ORDER", "summary": "UFO Burger with fries to Room 301"},
        "message": "yes",
        "correct_response": "I've placed your order for UFO Burger with fries. It will be delivered to Room 301 in approximately 25-30 minutes. Is there anything else you'd like?",
        "incorrect_response": "Great! Could you please specify which burger you'd like?",
        "why": "Must execute pending action on confirmation"
    },

    # Consistent guest info (Lumira failure - name contradiction)
    {
        "guest_context": {"name": "Sana", "room": "408"},
        "message": "what is my name?",
        "correct_response": "Your name is Sana, and you're staying in Room 408. How can I help you today?",
        "incorrect_response": "I can't access personal information like your name.",
        "why": "Guest context is available and should be used consistently"
    },
]
```

---

## 8. Error Handling

### 8.1 LLM Error Types

```python
class LLMErrorHandler:
    """Handle various LLM API errors gracefully"""

    ERROR_HANDLERS = {
        "rate_limit": {
            "retry": True,
            "max_retries": 3,
            "backoff": "exponential",
            "fallback": "queue_for_later"
        },
        "timeout": {
            "retry": True,
            "max_retries": 2,
            "backoff": "linear",
            "fallback": "use_cached_or_escalate"
        },
        "invalid_response": {
            "retry": True,
            "max_retries": 1,
            "upgrade_model": True,
            "fallback": "escalate"
        },
        "context_length": {
            "retry": True,
            "max_retries": 1,
            "action": "truncate_context",
            "fallback": "escalate"
        },
        "content_filter": {
            "retry": False,
            "action": "log_and_escalate",
            "fallback": "escalate"
        },
        "api_error": {
            "retry": True,
            "max_retries": 3,
            "backoff": "exponential",
            "fallback": "escalate"
        }
    }

    async def handle(self, error: Exception, context: dict) -> ErrorResult:
        error_type = self._classify_error(error)
        handler = self.ERROR_HANDLERS.get(error_type, self.ERROR_HANDLERS["api_error"])

        if handler["retry"]:
            for attempt in range(handler["max_retries"]):
                try:
                    await self._wait(attempt, handler["backoff"])

                    if handler.get("upgrade_model"):
                        context["model"] = "gpt-4.1"

                    if handler.get("action") == "truncate_context":
                        context = self._truncate_context(context)

                    return await self._retry(context)

                except Exception as e:
                    continue

        return await self._fallback(handler["fallback"], context)
```

### 8.2 Graceful Degradation

```python
FALLBACK_RESPONSES = {
    "rate_limited": {
        "message": "I'm experiencing high demand right now. Let me create a ticket and someone will get back to you shortly.",
        "action": "create_ticket"
    },
    "timeout": {
        "message": "That's taking longer than expected. Let me have our team look into this for you.",
        "action": "create_ticket"
    },
    "validation_failed": {
        "message": "I want to make sure I give you accurate information. Let me connect you with our team who can help directly.",
        "action": "escalate"
    },
    "unknown_error": {
        "message": "I apologize, but I'm having some technical difficulties. Would you like me to connect you with our guest services team?",
        "action": "offer_escalation"
    }
}
```

---

## 9. Cost Optimization

### 9.1 Cost Tracking

```python
class CostTracker:
    """Track LLM costs per conversation and hotel"""

    PRICING = {
        "gpt-4.1": {"input": 0.03, "output": 0.06},  # per 1K tokens
        "gpt-4.1-mini": {"input": 0.0015, "output": 0.002},
        "gpt-5-nano": {"input": 0.001, "output": 0.002},
        "text-embedding-3-small": {"input": 0.00002, "output": 0},
    }

    async def track(self, model: str, input_tokens: int, output_tokens: int) -> Cost:
        pricing = self.PRICING.get(model, self.PRICING["gpt-4.1-mini"])

        cost = Cost(
            input_cost=(input_tokens / 1000) * pricing["input"],
            output_cost=(output_tokens / 1000) * pricing["output"],
            model=model,
            timestamp=datetime.utcnow()
        )

        await self._save_to_db(cost)
        return cost

    async def get_conversation_cost(self, conversation_id: str) -> float:
        costs = await self._get_costs(conversation_id)
        return sum(c.total for c in costs)

    async def get_hotel_daily_cost(self, hotel_id: str, date: date) -> float:
        costs = await self._get_hotel_costs(hotel_id, date)
        return sum(c.total for c in costs)
```

### 9.2 Cost Optimization Strategies

```python
COST_OPTIMIZATION = {
    # 1. Model routing - use cheapest model that works
    "model_routing": {
        "simple_intents": ["greeting", "confirmation", "cancellation"],
        "simple_model": "gpt-4.1-mini",
        "complex_intents": ["complaint", "multi_intent"],
        "complex_model": "gpt-4.1"
    },

    # 2. Response caching
    "caching": {
        "faq_cache_ttl": 3600,  # 1 hour
        "embedding_cache_ttl": 86400 * 7,  # 7 days
        "capability_cache_ttl": 86400,  # 24 hours
    },

    # 3. Prompt optimization
    "prompt_optimization": {
        "max_history_messages": 10,
        "summarize_after_messages": 15,
        "max_rag_chunks": 3,
        "compress_capabilities": True,
    },

    # 4. Batching
    "batching": {
        "review_analysis": True,  # Batch review analysis
        "embedding_generation": True,  # Batch embeddings
    }
}
```

---

## 10. Testing & Evaluation

### 10.1 Test Categories

```python
LLM_TEST_SUITE = {
    # Lumira failure cases (CRITICAL)
    "lumira_failures": [
        "test_kadak_delivery_rejection",
        "test_multiple_menus",
        "test_hyderabad_cab_rejection",
        "test_confirmation_handling",
        "test_burger_in_room_context",
        "test_name_consistency",
        "test_ticket_update_not_create",
        "test_check_nearby_consistency",
        "test_full_menu_request",
    ],

    # Intent classification
    "intent_classification": [
        "test_order_intent",
        "test_menu_intent",
        "test_booking_intent",
        "test_escalation_intent",
        "test_confirmation_intent",
        "test_multi_intent",
        "test_ambiguous_intent",
    ],

    # Confidence scoring
    "confidence": [
        "test_high_confidence_proceed",
        "test_low_confidence_escalate",
        "test_medium_confidence_clarify",
    ],

    # Response validation
    "validation": [
        "test_capability_contradiction_caught",
        "test_history_contradiction_caught",
        "test_valid_response_passes",
    ],

    # RAG
    "rag": [
        "test_faq_retrieval",
        "test_menu_retrieval",
        "test_policy_retrieval",
        "test_no_hallucination",
    ],
}
```

### 10.2 Evaluation Metrics

```python
EVALUATION_METRICS = {
    "accuracy": {
        "intent_classification_accuracy": 0.95,  # Target
        "entity_extraction_accuracy": 0.90,
        "response_relevance": 0.90,
    },

    "safety": {
        "capability_violation_rate": 0.01,  # Max 1%
        "contradiction_rate": 0.01,
        "hallucination_rate": 0.02,
    },

    "efficiency": {
        "avg_response_time_ms": 2000,
        "avg_cost_per_conversation": 0.05,
        "cache_hit_rate": 0.30,
    },

    "user_experience": {
        "escalation_rate": 0.10,  # 10% max
        "clarification_rate": 0.15,
        "task_completion_rate": 0.85,
    }
}
```

### 10.3 Automated Testing

```python
@pytest.mark.asyncio
async def test_kadak_delivery_rejection():
    """
    Test Case 1 from Lumira failures:
    Bot should NOT promise delivery from Kadak (dine-in only)
    """
    context = create_test_context(hotel="ICONIQA_MUMBAI")

    # User asks for Kadak delivery
    response = await process_message(
        message="I want items from kadak delivered to my room",
        context=context
    )

    # Should NOT promise delivery
    assert "deliver" not in response.text.lower() or "cannot" in response.text.lower()
    assert "dine-in" in response.text.lower() or "restaurant" in response.text.lower()

    # Should offer alternatives
    assert "book a table" in response.text.lower() or "in-room dining" in response.text.lower()


@pytest.mark.asyncio
async def test_confirmation_handling():
    """
    Test Case 8 from Lumira failures:
    When user says "yes" after confirmation prompt, execute the action
    """
    context = create_test_context()

    # Set up pending confirmation
    context.state.pending_confirmation = {
        "action": "ORDER",
        "summary": "UFO Burger to Room 301",
        "details": {"item": "UFO Burger", "room": "301"}
    }

    # User confirms
    response = await process_message(
        message="yes",
        context=context
    )

    # Should execute order, not ask again
    assert "placed" in response.text.lower() or "confirmed" in response.text.lower()
    assert "which" not in response.text.lower()  # Should NOT ask again
    assert response.ticket_created is not None  # Should create ticket
```

---

## Appendix: Prompt Templates Reference

### Quick Reference

| Prompt | Purpose | Model | Avg Tokens |
|--------|---------|-------|------------|
| `INTENT_CLASSIFICATION_PROMPT` | Classify user intent | gpt-4.1-mini | 800 |
| `RESPONSE_GENERATION_PROMPT` | Generate reply | gpt-4.1-mini | 1200 |
| `VALIDATION_PROMPT` | Check response | gpt-4.1-mini | 600 |
| `QUERY_REWRITE_PROMPT` | Improve RAG query | gpt-4.1-mini | 200 |
| `RERANK_PROMPT` | Rerank RAG results | gpt-4.1-mini | 400 |
| `SUMMARIZATION_PROMPT` | Summarize history | gpt-4.1-mini | 500 |

---

*Document Last Updated: February 3, 2026*
