# KePSLA Conversational AI - Tech Stack Specification

**Document Version**: 1.0
**Created**: February 3, 2026
**Status**: Recommended

---

## Table of Contents

1. [Overview](#1-overview)
2. [Recommended Tech Stack](#2-recommended-tech-stack)
3. [Technology Comparisons](#3-technology-comparisons)
4. [Detailed Justifications](#4-detailed-justifications)
5. [Integration Architecture](#5-integration-architecture)
6. [Scalability Considerations](#6-scalability-considerations)
7. [Cost Estimates](#7-cost-estimates)
8. [Migration from Lumira](#8-migration-from-lumira)

---

## 1. Overview

### 1.1 Selection Criteria

Technologies were selected based on:
1. **Performance** - Low latency for real-time chat
2. **Scalability** - Handle 1000+ concurrent conversations
3. **Developer Experience** - Easy to develop and maintain
4. **Cost Efficiency** - Minimize infrastructure and API costs
5. **Ecosystem** - Strong community and library support
6. **Team Familiarity** - Leverage existing skills

### 1.2 Stack Summary

```
┌─────────────────────────────────────────────────────────────────┐
│                     TECH STACK OVERVIEW                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Frontend        │  React + TypeScript + Tailwind               │
│  ────────────────┼─────────────────────────────────────────────│
│  Backend         │  FastAPI (Python 3.11+)                      │
│  ────────────────┼─────────────────────────────────────────────│
│  LLM Provider    │  OpenAI (GPT-4.1-mini primary)               │
│  ────────────────┼─────────────────────────────────────────────│
│  Vector DB       │  FAISS (local) or Pinecone (cloud)           │
│  ────────────────┼─────────────────────────────────────────────│
│  Primary DB      │  PostgreSQL                                  │
│  ────────────────┼─────────────────────────────────────────────│
│  Cache/Sessions  │  Redis                                       │
│  ────────────────┼─────────────────────────────────────────────│
│  Message Queue   │  Redis (or RabbitMQ for scale)               │
│  ────────────────┼─────────────────────────────────────────────│
│  Infrastructure  │  AWS (ECS/EKS) or Railway                    │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## 2. Recommended Tech Stack

### 2.1 Complete Stack

| Layer | Technology | Version | Purpose |
|-------|------------|---------|---------|
| **Backend Framework** | FastAPI | 0.109+ | REST API, async support |
| **Language** | Python | 3.11+ | Core development |
| **LLM Provider** | OpenAI | API v1 | Intent, response generation |
| **LLM Framework** | LangChain | 0.1+ | LLM orchestration |
| **Vector Store** | FAISS | 1.12+ | RAG embeddings (local) |
| **Vector Store (Alt)** | Pinecone | - | RAG embeddings (cloud) |
| **Embeddings** | OpenAI | text-embedding-3-small | Vector embeddings |
| **Primary Database** | PostgreSQL | 15+ | Persistent storage |
| **ORM** | SQLAlchemy | 2.0+ | Database abstraction |
| **Cache** | Redis | 7+ | Sessions, caching |
| **Task Queue** | Celery | 5.3+ | Background jobs |
| **API Validation** | Pydantic | 2.0+ | Data validation |
| **HTTP Client** | httpx | 0.25+ | Async HTTP calls |
| **Testing** | pytest | 7+ | Unit/integration tests |
| **Containerization** | Docker | 24+ | Development, deployment |
| **Orchestration** | Docker Compose / K8s | - | Container management |
| **CI/CD** | GitHub Actions | - | Automated pipelines |
| **Monitoring** | Prometheus + Grafana | - | Metrics, dashboards |
| **Logging** | Structlog | 23+ | Structured logging |
| **API Docs** | OpenAPI (auto) | 3.1 | Auto-generated docs |

### 2.2 Frontend Stack (Admin Dashboard)

| Technology | Version | Purpose |
|------------|---------|---------|
| React | 18+ | UI framework |
| TypeScript | 5+ | Type safety |
| Tailwind CSS | 3+ | Styling |
| React Query | 5+ | Data fetching |
| Zustand | 4+ | State management |
| Vite | 5+ | Build tool |

### 2.3 Chat Widget Stack

| Technology | Purpose |
|------------|---------|
| Preact | Lightweight widget (3KB) |
| TypeScript | Type safety |
| CSS-in-JS | Scoped styles |
| WebSocket | Real-time messaging |

---

## 3. Technology Comparisons

### 3.1 Backend Framework: FastAPI vs Flask

| Criteria | FastAPI | Flask (Lumira) | Winner |
|----------|---------|----------------|--------|
| Async Support | Native | Requires extensions | FastAPI |
| Performance | High (Starlette) | Medium | FastAPI |
| Type Safety | Built-in (Pydantic) | Manual | FastAPI |
| Auto API Docs | Yes (Swagger/ReDoc) | Flasgger addon | FastAPI |
| Validation | Automatic | Manual | FastAPI |
| Learning Curve | Medium | Low | Flask |
| Community | Growing fast | Mature | Tie |
| WebSocket | Built-in | Flask-SocketIO | FastAPI |

**Decision**: **FastAPI** - Better async support crucial for chat apps, built-in validation matches our Pydantic-heavy design.

---

### 3.2 Database: PostgreSQL vs MySQL

| Criteria | PostgreSQL | MySQL (Lumira) | Winner |
|----------|------------|----------------|--------|
| JSON Support | Native, powerful | Basic | PostgreSQL |
| Full-text Search | Built-in | Basic | PostgreSQL |
| Async Drivers | asyncpg (fast) | aiomysql | PostgreSQL |
| Reliability | Excellent | Good | PostgreSQL |
| Advanced Types | Arrays, JSONB, UUID | Limited | PostgreSQL |
| Scalability | Excellent | Good | PostgreSQL |
| Team Familiarity | Medium | High | MySQL |

**Decision**: **PostgreSQL** - Superior JSON support for storing conversation state, better async performance.

**Migration Note**: If team prefers MySQL, it's still viable. Queries are mostly compatible.

---

### 3.3 Vector Database: FAISS vs Pinecone vs Weaviate

| Criteria | FAISS | Pinecone | Weaviate |
|----------|-------|----------|----------|
| Cost | Free | $70+/month | Free (self-host) |
| Setup | Local file | Cloud managed | Self-host or cloud |
| Scalability | Limited | Unlimited | Good |
| Latency | ~10ms | ~50ms | ~30ms |
| Filtering | Manual | Built-in | Built-in |
| Persistence | Manual | Automatic | Automatic |
| Operations | Low | Zero | Medium |

**Decision**:
- **Development/MVP**: **FAISS** (free, fast, sufficient for <1M vectors)
- **Production Scale**: **Pinecone** (managed, scalable, reliable)

---

### 3.4 LLM Provider: OpenAI vs Anthropic vs Google

| Criteria | OpenAI | Anthropic | Google (Gemini) |
|----------|--------|-----------|-----------------|
| Model Quality | Excellent | Excellent | Good |
| Function Calling | Best | Good | Good |
| Structured Output | Native | Via prompts | Via prompts |
| Pricing | $$ | $$$ | $ |
| Rate Limits | Generous | Limited | Generous |
| Latency | Low | Medium | Low |
| Team Experience | High | Low | Low |

**Decision**: **OpenAI** - Best function calling (critical for our handlers), structured output support, team familiarity.

---

### 3.5 LLM Model Selection by Task

| Task | Model | Cost/1K tokens | Why |
|------|-------|----------------|-----|
| Intent Classification | gpt-4.1-mini | $0.002 | Fast, cheap, accurate enough |
| Response Generation | gpt-4.1-mini | $0.002 | Good balance |
| Complex Reasoning | gpt-4.1 | $0.03 | When mini fails |
| Response Validation | gpt-4.1-mini | $0.002 | Simple yes/no checks |
| Embeddings | text-embedding-3-small | $0.00002 | Cost-effective |
| Review Analysis | gpt-5-nano | $0.001 | Batch processing |

**Cost Optimization Strategy**:
1. Use gpt-4.1-mini for 90% of requests
2. Fallback to gpt-4.1 only when confidence < 0.7
3. Cache common responses
4. Batch similar requests

---

## 4. Detailed Justifications

### 4.1 Why FastAPI over Flask?

```python
# Flask (Lumira) - Synchronous, blocking
@app.route('/message', methods=['POST'])
def handle_message():
    # This blocks the entire worker
    llm_response = openai.chat.completions.create(...)  # Waits here
    ticket = create_ticket(...)  # Waits here
    return jsonify(response)

# FastAPI - Asynchronous, non-blocking
@app.post('/message')
async def handle_message(request: MessageRequest):
    # Concurrent execution
    llm_task = asyncio.create_task(get_llm_response(...))
    context_task = asyncio.create_task(load_context(...))

    llm_response, context = await asyncio.gather(llm_task, context_task)
    return Response(...)
```

**Benefits**:
- 3-5x better throughput under load
- Native async/await for I/O bound operations
- Automatic request validation with Pydantic
- Auto-generated OpenAPI documentation

---

### 4.2 Why PostgreSQL for Conversation State?

```sql
-- Store complex conversation state as JSONB
CREATE TABLE conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    guest_id INTEGER NOT NULL,
    state JSONB NOT NULL DEFAULT '{}',
    -- Easy to query nested JSON
    created_at TIMESTAMP DEFAULT NOW()
);

-- Query specific state fields efficiently
SELECT * FROM conversations
WHERE state->>'current_intent' = 'ORDER'
AND state->'entities_collected' ? 'item_name';

-- Index JSON fields for performance
CREATE INDEX idx_conv_intent ON conversations ((state->>'current_intent'));
```

**Benefits**:
- Native JSON querying and indexing
- No need for separate state table
- Easy schema evolution

---

### 4.3 Why Pydantic 2.0?

```python
from pydantic import BaseModel, Field, field_validator
from typing import Optional
from enum import Enum

class IntentType(str, Enum):
    ORDER = "order"
    BOOKING = "booking"
    MENU = "menu"
    ESCALATION = "escalation"
    FAQ = "faq"

class ClassifiedIntent(BaseModel):
    intent_type: IntentType
    confidence: float = Field(ge=0.0, le=1.0)
    entities: dict = Field(default_factory=dict)
    requires_confirmation: bool = False

    @field_validator('confidence')
    @classmethod
    def check_confidence(cls, v):
        if v < 0.5:
            # Log low confidence for monitoring
            logger.warning(f"Low confidence intent: {v}")
        return v

# Automatic validation from LLM response
intent = ClassifiedIntent.model_validate(llm_output)
```

**Benefits**:
- 5-50x faster than Pydantic v1
- Native integration with FastAPI
- Automatic OpenAI structured output parsing

---

### 4.4 Why Redis for Sessions?

```python
import redis.asyncio as redis

class SessionManager:
    def __init__(self):
        self.redis = redis.Redis(host='localhost', port=6379, db=0)

    async def get_state(self, session_id: str) -> ConversationState:
        data = await self.redis.get(f"session:{session_id}")
        if data:
            return ConversationState.model_validate_json(data)
        return ConversationState()

    async def save_state(self, session_id: str, state: ConversationState):
        await self.redis.setex(
            f"session:{session_id}",
            3600,  # 1 hour TTL
            state.model_dump_json()
        )
```

**Benefits**:
- Sub-millisecond reads/writes
- Automatic expiration (TTL)
- Pub/Sub for real-time features
- Already used by Lumira (familiar)

---

## 5. Integration Architecture

### 5.1 External Service Integrations

```
┌─────────────────────────────────────────────────────────────────┐
│                    INTEGRATION ARCHITECTURE                      │
└─────────────────────────────────────────────────────────────────┘

                         ┌──────────────┐
                         │   WhatsApp   │
                         │  Cloud API   │
                         └──────┬───────┘
                                │ Webhooks
                                ▼
┌──────────────┐         ┌──────────────┐         ┌──────────────┐
│   OpenAI     │◄───────▶│   FastAPI    │◄───────▶│   KePSLA     │
│   API        │         │   Backend    │         │  Ticket API  │
└──────────────┘         └──────┬───────┘         └──────────────┘
                                │
              ┌─────────────────┼─────────────────┐
              │                 │                 │
              ▼                 ▼                 ▼
       ┌──────────┐      ┌──────────┐      ┌──────────┐
       │PostgreSQL│      │  Redis   │      │  FAISS   │
       │          │      │          │      │  Index   │
       └──────────┘      └──────────┘      └──────────┘
```

### 5.2 API Integration Patterns

| Integration | Pattern | Retry Strategy |
|-------------|---------|----------------|
| OpenAI | Async HTTP | Exponential backoff, 3 retries |
| WhatsApp | Webhooks + REST | Queue-based retry |
| KePSLA Ticketing | REST | Circuit breaker, 3 retries |
| Database | Connection pool | Auto-reconnect |
| Redis | Connection pool | Auto-reconnect |

---

## 6. Scalability Considerations

### 6.1 Scaling Strategy

| Load Level | Conversations/min | Infrastructure |
|------------|-------------------|----------------|
| Low | 0-100 | Single server, 2 workers |
| Medium | 100-500 | 2 servers, load balancer |
| High | 500-2000 | 4+ servers, K8s autoscaling |
| Enterprise | 2000+ | Microservices, regional deploy |

### 6.2 Bottleneck Analysis

| Component | Bottleneck | Solution |
|-----------|------------|----------|
| LLM API | Rate limits, latency | Caching, model routing |
| Database | Connection limits | Connection pooling, read replicas |
| Redis | Memory | Cluster mode, TTL policies |
| Vector DB | Query latency | Pinecone, index sharding |

### 6.3 Caching Strategy

```python
CACHE_CONFIG = {
    # Cache FAQ responses (high hit rate)
    "faq_responses": {
        "ttl": 3600,  # 1 hour
        "max_size": 1000
    },
    # Cache capability checks (static)
    "capabilities": {
        "ttl": 86400,  # 24 hours
        "max_size": 100
    },
    # Cache embeddings (expensive to compute)
    "embeddings": {
        "ttl": 604800,  # 7 days
        "max_size": 10000
    }
}
```

---

## 7. Cost Estimates

### 7.1 Monthly Infrastructure Costs

| Component | Service | Estimated Cost |
|-----------|---------|----------------|
| Compute | AWS ECS (2 tasks) | $50-100 |
| Database | RDS PostgreSQL (db.t3.medium) | $30-50 |
| Cache | ElastiCache Redis (cache.t3.micro) | $15-25 |
| Storage | S3 + EBS | $10-20 |
| Monitoring | CloudWatch | $10-20 |
| **Total Infrastructure** | | **$115-215/month** |

### 7.2 API Costs (Per 1000 Conversations)

| API | Usage | Cost |
|-----|-------|------|
| OpenAI (gpt-4.1-mini) | ~50K tokens | $0.10 |
| OpenAI (embeddings) | ~10K tokens | $0.0002 |
| WhatsApp API | 1000 messages | $0-5 (template dependent) |
| **Total per 1K conversations** | | **~$0.15-5** |

### 7.3 Cost Comparison: Lumira vs New Bot

| Metric | Lumira | New Bot | Savings |
|--------|--------|---------|---------|
| LLM calls per message | 4-6 | 2-3 | 50% |
| Avg tokens per call | 2000 | 1500 | 25% |
| Cost per conversation | $0.08 | $0.04 | 50% |

---

## 8. Migration from Lumira

### 8.1 Compatibility Matrix

| Lumira Component | Migration Path |
|------------------|----------------|
| MySQL data | Export → PostgreSQL import |
| Redis sessions | Compatible (same structure) |
| FAISS indexes | Rebuild with new schema |
| API endpoints | New endpoints (breaking change) |
| WhatsApp integration | Reuse webhook config |

### 8.2 Data Migration Steps

1. **Export MySQL data**
   ```bash
   mysqldump -u user -p lumira_db > lumira_backup.sql
   ```

2. **Transform for PostgreSQL**
   ```bash
   pgloader mysql://user@host/lumira postgresql://user@host/newbot
   ```

3. **Rebuild vector indexes**
   ```python
   # Re-embed all hotel data with new schema
   python scripts/rebuild_faiss_index.py
   ```

4. **Migrate sessions** (optional - can start fresh)
   ```python
   # Sessions expire anyway, fresh start recommended
   ```

### 8.3 Rollback Plan

| Phase | Rollback Action |
|-------|-----------------|
| Development | N/A (separate environment) |
| Staging | Switch DNS back to Lumira |
| Production | Blue-green deployment, instant rollback |

---

## Appendix: Package Versions

### requirements.txt

```
# Core
fastapi==0.109.0
uvicorn[standard]==0.27.0
python-dotenv==1.0.0

# Database
sqlalchemy==2.0.25
asyncpg==0.29.0
alembic==1.13.1

# Cache
redis==5.0.1

# LLM
openai==1.10.0
langchain==0.1.4
langchain-openai==0.0.5
tiktoken==0.5.2

# Vector Store
faiss-cpu==1.7.4

# Validation
pydantic==2.5.3
pydantic-settings==2.1.0

# HTTP
httpx==0.26.0

# Task Queue
celery==5.3.6

# Testing
pytest==7.4.4
pytest-asyncio==0.23.3
pytest-cov==4.1.0

# Monitoring
prometheus-client==0.19.0
structlog==24.1.0

# Development
black==24.1.0
ruff==0.1.14
mypy==1.8.0
pre-commit==3.6.0
```

---

*Document Last Updated: February 3, 2026*
