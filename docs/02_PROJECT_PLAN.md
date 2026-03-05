# KePSLA Conversational AI - Project Plan

**Document Version**: 1.0
**Created**: February 3, 2026
**Status**: Planning Phase

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Goals & Objectives](#2-goals--objectives)
3. [Scope Definition](#3-scope-definition)
4. [Phased Development Plan](#4-phased-development-plan)
5. [Detailed Task Breakdown](#5-detailed-task-breakdown)
6. [Dependencies & Risks](#6-dependencies--risks)
7. [Success Criteria](#7-success-criteria)
8. [Milestones & Deliverables](#8-milestones--deliverables)
9. [Team & Resources](#9-team--resources)

---

## 1. Project Overview

### 1.1 Background

KePSLA's current Lumira chatbot has fundamental architectural flaws causing:
- Contradictory responses to users
- Lost conversation context
- Duplicate ticket creation
- No human escalation capability
- No confidence tracking

### 1.2 Project Purpose

Build a **new conversational AI platform** from scratch that:
- Fully complies with PRD requirements
- Fixes all 12 identified problems in Lumira
- Implements confidence-aware responses
- Supports human-in-the-loop escalation
- Is configurable and industry-agnostic

### 1.3 Project Name

**Project Codename**: `KePSLA Bot v2` (or choose: Nova, Atlas, Aria)

---

## 2. Goals & Objectives

### 2.1 Primary Goals

| # | Goal | Success Metric |
|---|------|----------------|
| 1 | Zero contradictory responses | <1% contradiction rate |
| 2 | Complete conversation context | 100% context retention |
| 3 | Smart ticket management | 0 duplicate tickets |
| 4 | Human escalation ready | <30 sec handoff time |
| 5 | PRD compliant | 100% requirement coverage |

### 2.2 Secondary Goals

| # | Goal | Success Metric |
|---|------|----------------|
| 6 | Faster response time | <2 seconds average |
| 7 | Lower LLM costs | <$0.05 per conversation |
| 8 | High availability | 99.5% uptime |
| 9 | Easy configuration | Admin UI for changes |
| 10 | Multi-language support | 5+ languages |

---

## 3. Scope Definition

### 3.1 In Scope

#### Core Features
- [x] Conversation state machine
- [x] Intent classification with confidence scoring
- [x] Capability registry (configurable)
- [x] Response validation layer
- [x] Human escalation (live chat, ticket, callback)
- [x] Multi-menu/multi-item support
- [x] Confirmation handling ("yes"/"no")
- [x] Smart ticket create/update logic
- [x] RAG-based knowledge retrieval
- [x] Guest context management

#### Integrations
- [x] WhatsApp Business API
- [x] Website chat widget
- [x] KePSLA Ticketing System
- [x] MySQL database
- [x] Redis session store

#### Use Cases (from PRD)
- [x] FAQ & Information
- [x] Lead Capture
- [x] Complaint / Support Ticket
- [x] Callback Request
- [x] Human Escalation

### 3.2 Out of Scope (Phase 1)

- [ ] Voice/Speech support
- [ ] Mobile app integration
- [ ] Multi-tenant SaaS architecture
- [ ] Advanced analytics dashboard
- [ ] A/B testing framework
- [ ] Custom ML model training

---

## 4. Phased Development Plan

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         DEVELOPMENT TIMELINE                                 │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Week 1-2        Week 3-4        Week 5-6        Week 7-8        Week 9-10  │
│  ─────────       ─────────       ─────────       ─────────       ───────── │
│  ┌───────┐       ┌───────┐       ┌───────┐       ┌───────┐       ┌───────┐ │
│  │Phase 1│──────▶│Phase 2│──────▶│Phase 3│──────▶│Phase 4│──────▶│Phase 5│ │
│  │ Core  │       │Intent │       │Handlers│      │Integr.│       │Testing│ │
│  │ Arch  │       │Response│      │Business│      │  APIs │       │Deploy │ │
│  └───────┘       └───────┘       └───────┘       └───────┘       └───────┘ │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Phase Overview

| Phase | Name | Duration | Focus |
|-------|------|----------|-------|
| 1 | Core Architecture | Week 1-2 | Foundation, state machine, capability registry |
| 2 | Intent & Response | Week 3-4 | Classification, validation, confidence |
| 3 | Handlers | Week 5-6 | Menu, order, booking, ticket, escalation |
| 4 | Integrations | Week 7-8 | WhatsApp, APIs, database |
| 5 | Testing & Deploy | Week 9-10 | QA, UAT, production deployment |

---

## 5. Detailed Task Breakdown

### Phase 1: Core Architecture (Week 1-2)

#### Week 1: Foundation

| Task ID | Task | Priority | Effort |
|---------|------|----------|--------|
| 1.1.1 | Set up project repository | P0 | 2h |
| 1.1.2 | Configure development environment | P0 | 4h |
| 1.1.3 | Set up FastAPI project structure | P0 | 4h |
| 1.1.4 | Configure linting, formatting, pre-commit | P1 | 2h |
| 1.1.5 | Set up Docker development environment | P1 | 4h |
| 1.1.6 | Create base configuration management | P0 | 4h |
| 1.1.7 | Set up logging and monitoring | P1 | 4h |

#### Week 2: Core Components

| Task ID | Task | Priority | Effort |
|---------|------|----------|--------|
| 1.2.1 | Implement Capability Registry | P0 | 8h |
| 1.2.2 | Implement Conversation State Machine | P0 | 16h |
| 1.2.3 | Implement Unified Context Builder | P0 | 8h |
| 1.2.4 | Create base Pydantic models | P0 | 4h |
| 1.2.5 | Set up Redis session storage | P0 | 4h |
| 1.2.6 | Write unit tests for core components | P0 | 8h |

**Phase 1 Deliverables:**
- [ ] Working project structure
- [ ] Capability Registry with hotel config
- [ ] Conversation State Machine
- [ ] Context Builder
- [ ] Redis session management
- [ ] 80%+ test coverage for core

---

### Phase 2: Intent & Response System (Week 3-4)

#### Week 3: Intent Classification

| Task ID | Task | Priority | Effort |
|---------|------|----------|--------|
| 2.1.1 | Create LLM client wrapper | P0 | 4h |
| 2.1.2 | Design intent taxonomy | P0 | 4h |
| 2.1.3 | Implement Intent Classifier | P0 | 12h |
| 2.1.4 | Implement Confidence Scoring | P0 | 8h |
| 2.1.5 | Create intent classification prompts | P0 | 8h |
| 2.1.6 | Add few-shot examples | P1 | 4h |

#### Week 4: Response & Validation

| Task ID | Task | Priority | Effort |
|---------|------|----------|--------|
| 2.2.1 | Implement Response Generator | P0 | 12h |
| 2.2.2 | Implement Response Validator | P0 | 12h |
| 2.2.3 | Implement Fallback Handler | P0 | 8h |
| 2.2.4 | Create validation rules engine | P1 | 8h |
| 2.2.5 | Write integration tests | P0 | 8h |

**Phase 2 Deliverables:**
- [ ] Intent Classifier with confidence scores
- [ ] Response Generator
- [ ] Response Validator (catches contradictions)
- [ ] Fallback Handler with hierarchy
- [ ] All prompts documented

---

### Phase 3: Handlers & Business Logic (Week 5-6)

#### Week 5: Core Handlers

| Task ID | Task | Priority | Effort |
|---------|------|----------|--------|
| 3.1.1 | Implement Menu Handler (multi-menu) | P0 | 12h |
| 3.1.2 | Implement Order Handler (with confirmation) | P0 | 12h |
| 3.1.3 | Implement Booking Handler | P0 | 8h |
| 3.1.4 | Implement FAQ Handler with RAG | P0 | 8h |

#### Week 6: Advanced Handlers

| Task ID | Task | Priority | Effort |
|---------|------|----------|--------|
| 3.2.1 | Implement Ticket Handler (smart create/update) | P0 | 16h |
| 3.2.2 | Implement Human Escalation Handler | P0 | 12h |
| 3.2.3 | Implement Callback Handler | P1 | 4h |
| 3.2.4 | Implement Lead Capture Handler | P1 | 4h |
| 3.2.5 | Write handler tests | P0 | 8h |

**Phase 3 Deliverables:**
- [ ] Menu Handler (sends multiple menus)
- [ ] Order Handler (tracks confirmations)
- [ ] Booking Handler
- [ ] Ticket Handler (no duplicates)
- [ ] Escalation Handler (live + async)
- [ ] All handlers tested

---

### Phase 4: Integrations (Week 7-8)

#### Week 7: External APIs

| Task ID | Task | Priority | Effort |
|---------|------|----------|--------|
| 4.1.1 | WhatsApp Business API integration | P0 | 16h |
| 4.1.2 | KePSLA Ticketing API integration | P0 | 12h |
| 4.1.3 | Implement webhook handlers | P0 | 8h |
| 4.1.4 | Add retry logic and error handling | P0 | 4h |

#### Week 8: Database & Storage

| Task ID | Task | Priority | Effort |
|---------|------|----------|--------|
| 4.2.1 | Set up MySQL database | P0 | 4h |
| 4.2.2 | Implement database models | P0 | 8h |
| 4.2.3 | Implement conversation logging | P0 | 8h |
| 4.2.4 | Set up FAISS/vector storage | P0 | 8h |
| 4.2.5 | Implement RAG data pipeline | P0 | 8h |
| 4.2.6 | Integration testing | P0 | 8h |

**Phase 4 Deliverables:**
- [ ] WhatsApp integration working
- [ ] Ticketing API integration
- [ ] Database models and queries
- [ ] RAG pipeline operational
- [ ] End-to-end flow working

---

### Phase 5: Testing & Deployment (Week 9-10)

#### Week 9: Testing

| Task ID | Task | Priority | Effort |
|---------|------|----------|--------|
| 5.1.1 | Test all 9 Lumira failure cases | P0 | 16h |
| 5.1.2 | Load testing (100 concurrent users) | P0 | 8h |
| 5.1.3 | Security testing | P0 | 8h |
| 5.1.4 | UAT with hotel staff | P0 | 16h |

#### Week 10: Deployment

| Task ID | Task | Priority | Effort |
|---------|------|----------|--------|
| 5.2.1 | Set up production infrastructure | P0 | 8h |
| 5.2.2 | Configure CI/CD pipeline | P0 | 8h |
| 5.2.3 | Deploy to staging | P0 | 4h |
| 5.2.4 | Production deployment | P0 | 4h |
| 5.2.5 | Monitoring and alerting setup | P0 | 8h |
| 5.2.6 | Documentation and handoff | P0 | 8h |

**Phase 5 Deliverables:**
- [ ] All 9 failure cases pass
- [ ] Load test results documented
- [ ] Security audit passed
- [ ] UAT sign-off
- [ ] Production deployment live
- [ ] Runbook and documentation

---

## 6. Dependencies & Risks

### 6.1 Dependencies

| Dependency | Owner | Status | Risk if Delayed |
|------------|-------|--------|-----------------|
| OpenAI API access | DevOps | Ready | High - blocks all LLM work |
| WhatsApp Business API | Business | Pending | High - blocks Phase 4 |
| KePSLA Ticketing API docs | Backend Team | Ready | Medium - blocks integration |
| Hotel capability data | Hotel Ops | Pending | Medium - needed for testing |
| Redis infrastructure | DevOps | Ready | Medium - needed Phase 1 |
| MySQL database | DevOps | Ready | Medium - needed Phase 4 |

### 6.2 Risks & Mitigation

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| LLM API rate limits | Medium | High | Implement caching, use cheaper models |
| WhatsApp API delays | Medium | High | Start integration early, have fallback |
| Scope creep | High | Medium | Strict change control process |
| Team availability | Medium | Medium | Cross-train team members |
| Performance issues | Low | High | Load test early and often |
| Security vulnerabilities | Low | Critical | Security review at each phase |

---

## 7. Success Criteria

### 7.1 Functional Criteria

| Criteria | Target | Measurement |
|----------|--------|-------------|
| All PRD use cases work | 100% | Checklist verification |
| No contradictory responses | <1% | Automated testing |
| Context retention | 100% | Multi-turn test scenarios |
| Escalation works | 100% | Manual testing |
| Multiple menus sent | 100% | Test with 3 menu request |
| Confirmation handling | 100% | "Yes"/"No" test cases |
| No duplicate tickets | 0 duplicates | Ticket log analysis |

### 7.2 Non-Functional Criteria

| Criteria | Target | Measurement |
|----------|--------|-------------|
| Response time | <2 seconds | P95 latency |
| Availability | 99.5% | Uptime monitoring |
| Concurrent users | 100+ | Load testing |
| Cost per conversation | <$0.05 | Token tracking |

### 7.3 Lumira Failure Case Tests

All 9 documented failures must pass:

| # | Test Case | Expected Result |
|---|-----------|-----------------|
| 1 | Kadak delivery request | Correctly says "dine-in only" from start |
| 2 | Request 3 menus | Sends all 3 menu URLs |
| 3 | Hyderabad cab request | Says "Mumbai only" immediately |
| 4 | "Can you check nearby?" | Consistent answer (yes or no) |
| 5 | "I want entire menu" | Sends complete menu, not asking to choose |
| 6 | "burger" → "room" | Understands "burger in room" |
| 7 | Table reservation follow-up | Updates existing ticket |
| 8 | "yes" to confirm order | Confirms and creates ticket |
| 9 | "what is my name?" | Correctly recalls guest name |

---

## 8. Milestones & Deliverables

### 8.1 Milestone Schedule

```
Week 2  ──────────  M1: Core Architecture Complete
                    ├── State Machine working
                    ├── Capability Registry configured
                    └── Context Builder ready

Week 4  ──────────  M2: Intent System Complete
                    ├── Intent Classifier with confidence
                    ├── Response Validator working
                    └── Fallback Handler ready

Week 6  ──────────  M3: All Handlers Complete
                    ├── All 6 handlers implemented
                    ├── Multi-menu support working
                    └── Confirmation tracking working

Week 8  ──────────  M4: Integrations Complete
                    ├── WhatsApp integration live
                    ├── Ticketing API connected
                    └── Database operational

Week 10 ──────────  M5: Production Launch
                    ├── All tests passing
                    ├── UAT approved
                    └── Live in production
```

### 8.2 Deliverables by Phase

| Phase | Deliverables |
|-------|--------------|
| Phase 1 | Core framework, State Machine, Capability Registry |
| Phase 2 | Intent Classifier, Response Validator, Confidence System |
| Phase 3 | All Handlers (Menu, Order, Booking, Ticket, Escalation) |
| Phase 4 | WhatsApp integration, Ticketing integration, Database |
| Phase 5 | Test reports, Documentation, Production deployment |

---

## 9. Team & Resources

### 9.1 Team Structure

| Role | Count | Responsibilities |
|------|-------|------------------|
| Tech Lead | 1 | Architecture, code review, decisions |
| Backend Developer | 2 | API, handlers, integrations |
| AI/ML Engineer | 1 | LLM prompts, RAG, confidence scoring |
| QA Engineer | 1 | Testing, automation |
| DevOps | 1 (part-time) | Infrastructure, CI/CD |

### 9.2 Tools & Infrastructure

| Category | Tool | Purpose |
|----------|------|---------|
| Repository | GitHub | Code hosting, PRs |
| CI/CD | GitHub Actions | Automated testing, deployment |
| Project Management | Linear/Jira | Task tracking |
| Communication | Slack | Team communication |
| Documentation | Notion/Confluence | Documentation |
| Monitoring | Datadog/CloudWatch | Production monitoring |

---

## Appendix: Quick Reference

### Key Dates

| Date | Event |
|------|-------|
| Week 1 | Project Kickoff |
| Week 2 | M1: Core Architecture |
| Week 4 | M2: Intent System |
| Week 6 | M3: All Handlers |
| Week 8 | M4: Integrations |
| Week 9 | UAT Testing |
| Week 10 | Production Launch |

### Contact Points

| Area | Contact |
|------|---------|
| Project Lead | TBD |
| Technical Questions | TBD |
| Business Requirements | TBD |
| Infrastructure | TBD |

---

*Document Last Updated: February 3, 2026*
