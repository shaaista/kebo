# KePSLA Conversational AI - Research Analysis
## New Bot Development Documentation

**Created**: February 3, 2026  
**Status**: FINAL

## Executive Summary

### Key Findings

- Lumira designed as per-message processor, not conversation-aware
- 12 critical architectural problems identified  
- 9 documented failure cases from real users
- 7 major missing components vs PRD requirements
- **Recommendation**: Build new bot (80% rewrite needed anyway)

## Problem Categorization

### P0 - Critical Problems (6)

**A. No State Machine**: User: "burger" THEN "room" THEN Bot forgets burger
**B. No Intent Verification**: "IN_SCOPE" then "I can't help"  
**C. No Capability Registry**: Bot promises Kadak delivery (dine-in only)
**D. No Response Validation**: "I'll check" then "I can't check"
**E. Short History**: Only 10 messages (2-3 min conversation)
**F. Weak Ticket Matching**: Duplicate tickets 5090 AND 5091

### P1 - High Priority (4)

**G. Single Menu URL**: 30% of users ask for multiple menus
**H. No Confirmation**: Bot repeats question after "yes"
**I. Context Inconsistent**: Bot knows name, then forgets
**J. No Fallback**: Dead-end responses

### P2 - Medium (2)

**K. Contradictory Prompts**: Different personalities
**L. No Few-Shot Examples**: Unclear patterns

## Gap Analysis

| Requirement | PRD | Lumira | Gap |
|-------------|-----|--------|-----|
| State Machine | Required | Missing | CRITICAL |
| Capability Registry | Required | Hardcoded | CRITICAL |
| Confidence Scoring | Required | Missing | CRITICAL |
| Response Validation | Required | Missing | CRITICAL |
| Human Escalation | Required | Missing | CRITICAL |
| Multi-item Support | Required | Single | CRITICAL |
| Confirmation | Required | Missing | CRITICAL |

### Missing Components

Total LOC needed: approximately 3700 lines

1. State Machine (800 LOC)
2. Capability Registry (400 LOC)
3. Confidence Scoring (300 LOC)
4. Response Validator (500 LOC)
5. Human Escalation (600 LOC)
6. Multi-Menu Support (200 LOC)
7. Confirmation Handler (300 LOC)

## Industry Best Practices

### 1. Stateful Conversation
All major systems track: intent, entities, confirmations

### 2. Confidence-Based Routing
- Confidence >= 0.85: Proceed
- 0.50 <= Confidence < 0.85: Clarify  
- Confidence < 0.50: Escalate to human

### 3. Declarative Capabilities
Config-based, not hardcoded prompts

### 4. Response Validation
Check against capabilities before sending

### 5. Human Escalation (Core)
Multiple paths: explicit, implicit, behavioral

### 6. Multi-Item Support
Natural handling of multiple items

### 7. Confirmation Tracking
State-based across messages

### 8. Loop Detection
Auto-escalate on repetition

### 9. Semantic Similarity
Embeddings for matching

### 10. Configurable Workflows
No code changes needed

## Recommendations

### Decision: Fix or Rebuild?

**BUILD NEW BOT** (Recommended)

- Lumira needs 80% rewrite anyway
- New bot: 3-4 weeks
- Patches: 4-5 weeks
- New = cleaner, maintainable

## New Bot Architecture

### Principle 1: State-First
Track: intent, entities, confirmations, confidence

### Principle 2: Capability Registry
Config-based capabilities with enable/disable flags

### Principle 3: Confidence-Based Routing
Low score = escalate to human

### Principle 4: Response Validation
Validate before sending to user

### Principle 5: Multiple Escalation Paths
Explicit, implicit, behavioral, contextual

## Tech Stack

| Component | Choice | Why |
|-----------|--------|-----|
| Framework | FastAPI | Async, validation |
| LLM | OpenAI | Proven, structured |
| Vector DB | Pinecone | Production-ready |
| State | Redis | Fast expiring keys |
| Database | PostgreSQL | JSONB support |
| Async | Celery | Keep it working |
| Deploy | Docker + K8s | Scalable |

## Implementation

### Phase 1 (Week 1): Core Architecture
- State Machine
- Capability Registry
- Unified Context
- Base Prompt

### Phase 2 (Week 2): Safety
- Intent + Confidence
- Response Validator
- Fallback Handler

### Phase 3 (Week 2-3): Handlers
- Menu, Order, Booking
- Ticket (semantic)
- Escalation

### Phase 4 (Week 3): Integration
- WhatsApp, API, DB
- Admin Config

### Phase 5 (Week 4): Testing
- Test all 9 failure cases
- Load testing
- UAT with hotel staff
- Security audit

## Success Metrics

### Quality Targets
- Success rate: 75% THEN 95%
- Contradictions: 5% THEN <1%
- Duplicate tickets: 20% THEN <5%
- User satisfaction: 3.1 THEN 4.2/5

### Technical Targets
- Response time: 3-5s THEN <2s
- Availability: 95% THEN 99.5%
- Cost per chat: $0.08 THEN <$0.05
- Manual review: 30% THEN <10%

## Failure Case Analysis

| Case | Root Cause | Symptom |
|------|-----------|---------|
| 1 | No capability registry | Kadak delivery denied |
| 2 | Single menu URL | Can't get 3 menus |
| 3 | No capability check | Cab to Hyderabad |
| 4 | No response validator | Contradiction |
| 5 | Single menu URL | Menu frustration |
| 6 | No state machine | Lost burger |
| 7 | Weak matching | Duplicate tickets |
| 8 | No confirmation | Ignored yes |
| 9 | Inconsistent context | Name contradiction |

## Next Steps

1. Review with stakeholders
2. Approve new bot development
3. Allocate team resources
4. Schedule architecture kickoff
5. Begin Phase 1 implementation

---

**Status**: FINAL - Ready for Review
**Date**: February 3, 2026
**Prepared by**: Research AND Analysis Team
**Approval**: Pending
