# MySQL → PostgreSQL Migration Plan

**Status:** Planning — no code changes yet

---

## Databases

| | Dev | Prod |
|---|---|---|
| **MySQL (source)** | `172.16.5.32:3306/GHN_PROD_BAK` (VPN required) | Same |
| **PostgreSQL (destination)** | `35.154.99.170:5433/bsp_platform` (VPN required) | Prod credentials needed |
| **Schema** | `guest_chatbot` | `guest_chatbot` |
| **User** | `nexoria` | TBD |
| **Password** | `nexoria123` | TBD |

**Note:** VPN required for both source and destination databases.  
**Note:** Prod PostgreSQL credentials needed from developer before running prod migration.

---

## What We Are Doing

- **Copying** all data from MySQL → PostgreSQL (MySQL data stays intact, nothing deleted)
- **Creating** new tables in PostgreSQL `guest_chatbot` schema
- **Also migrating** locally stored file data into proper database tables
- **Code changes** to point the app to PostgreSQL after migration

---

## Part 1 — MySQL Tables to Copy (17 tables)

All existing tables move from MySQL into `guest_chatbot` schema in PostgreSQL:

| MySQL Table | PostgreSQL Target |
|---|---|
| `new_bot_hotels` | `guest_chatbot.new_bot_hotels` |
| `new_bot_restaurants` | `guest_chatbot.new_bot_restaurants` |
| `new_bot_menu_items` | `guest_chatbot.new_bot_menu_items` |
| `new_bot_guests` | `guest_chatbot.new_bot_guests` |
| `new_bot_bookings` | `guest_chatbot.new_bot_bookings` |
| `new_bot_orders` | `guest_chatbot.new_bot_orders` |
| `new_bot_order_items` | `guest_chatbot.new_bot_order_items` |
| `new_bot_conversations` | `guest_chatbot.new_bot_conversations` |
| `new_bot_messages` | `guest_chatbot.new_bot_messages` |
| `new_bot_business_config` | `guest_chatbot.new_bot_business_config` |
| `new_bot_capabilities` | `guest_chatbot.new_bot_capabilities` |
| `new_bot_intents` | `guest_chatbot.new_bot_intents` |
| `new_bot_services` | `guest_chatbot.new_bot_services` |
| `new_bot_prompt_registry` | `guest_chatbot.new_bot_prompt_registry` |
| `new_bot_kb_files` | `guest_chatbot.new_bot_kb_files` |
| `bot_hotel_images_scraper` | `guest_chatbot.bot_hotel_images_scraper` |
| `scrape_jobs` | `guest_chatbot.scrape_jobs` |

---

## Part 2 — Local File Data to Move to PostgreSQL (NEW tables)

These currently live as JSON/flat files on the server. They should become proper DB tables.

### Table 1: `guest_chatbot.widget_deployments`
**Currently in:** `config/widget_deployments.json`  
**Managed by:** `services/widget_deployment_service.py`

```sql
CREATE TABLE guest_chatbot.widget_deployments (
    id          SERIAL PRIMARY KEY,
    widget_key  VARCHAR(50) UNIQUE NOT NULL,
    hotel_code  VARCHAR(50) NOT NULL,
    name        VARCHAR(255),
    status      VARCHAR(20) DEFAULT 'active',
    allowed_origins JSON,
    theme       JSON,
    size        JSON,
    position    VARCHAR(20),
    bot_name    VARCHAR(255),
    phase       VARCHAR(50),
    auto_open   BOOLEAN DEFAULT false,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
```

---

### Table 2: `guest_chatbot.local_tickets`
**Currently in:** `data/ticketing/local_tickets.json`  
**Managed by:** `services/ticketing_service.py` (fallback when ticketing API is down)

```sql
CREATE TABLE guest_chatbot.local_tickets (
    id              SERIAL PRIMARY KEY,
    ticket_id       VARCHAR(100) UNIQUE,
    hotel_code      VARCHAR(50),
    status          VARCHAR(50),
    category        VARCHAR(100),
    sub_category    VARCHAR(100),
    priority        VARCHAR(50),
    room_number     VARCHAR(20),
    department_id   VARCHAR(100),
    assigned_id     VARCHAR(100),
    source          VARCHAR(100),
    payload_json    JSON,
    response_json   JSON,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
```

---

### Table 3: `guest_chatbot.conversation_contexts`
**Currently in:** `data/runtime/local_contexts.json` (5.1 MB — grows fast)  
**Managed by:** `core/context_manager.py` — written on every single chat turn

```sql
CREATE TABLE guest_chatbot.conversation_contexts (
    id              SERIAL PRIMARY KEY,
    session_id      VARCHAR(255) UNIQUE NOT NULL,
    hotel_code      VARCHAR(50) NOT NULL,
    guest_phone     VARCHAR(30),
    guest_name      VARCHAR(255),
    room_number     VARCHAR(20),
    channel         VARCHAR(50),
    state           VARCHAR(50),
    pending_action  VARCHAR(255),
    pending_data    JSON,
    messages        JSON,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    expires_at      TIMESTAMPTZ
);
```

---

### Table 4: `guest_chatbot.rag_chunks`
**Currently in:** `data/rag/local_index.json` (1.8 MB — grows with KB uploads)  
**Managed by:** `services/rag_service.py`

```sql
CREATE TABLE guest_chatbot.rag_chunks (
    id              VARCHAR(100) PRIMARY KEY,
    tenant_id       VARCHAR(100) NOT NULL,
    hotel_code      VARCHAR(50),
    business_type   VARCHAR(50),
    source          VARCHAR(255),
    chunk_index     INTEGER,
    chunk_id        VARCHAR(255),
    section         VARCHAR(255),
    content         TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
```

---

## What "Scripts for Prod and Dev" Means

Your developer wants the migration in separate runnable scripts, not manual work. Here are the 3 scripts needed:

### Script 1: `scripts/create_schema.py`
- Connects to PostgreSQL only
- Creates `guest_chatbot` schema
- Creates all 21 tables (17 copied + 4 new) with correct PostgreSQL column types
- Safe to re-run — uses `IF NOT EXISTS`
- Run this first before any data migration

### Script 2: `scripts/migrate_mysql_to_postgres.py`
- Connects to BOTH MySQL (read) and PostgreSQL (write)
- Copies all rows from all 17 MySQL tables into PostgreSQL
- Handles type differences: `LONGTEXT→TEXT`, `TINYINT→BOOLEAN`, `JSON` fields
- Idempotent — skips rows already migrated (safe to re-run)
- Has `--env dev` or `--env prod` flag

### Script 3: `scripts/migrate_files_to_postgres.py`
- Reads local JSON files (`widget_deployments.json`, `local_tickets.json`, `local_contexts.json`, `local_index.json`)
- Inserts them into the 4 new PostgreSQL tables
- Run once after Script 2

### Environment Files (what developer means by "keep env separate")
```
.env.dev   →  DEV MySQL credentials + DEV PostgreSQL credentials
.env.prod  →  PROD MySQL credentials + PROD PostgreSQL credentials
```

Run dev migration:
```bash
python scripts/migrate_mysql_to_postgres.py --env dev
```

Run prod migration (when ready):
```bash
python scripts/migrate_mysql_to_postgres.py --env prod
```

---

## Full Step-by-Step Plan

### Phase 1 — Setup (no downtime, dev only)
1. Run `scripts/create_schema.py --env dev`
2. Verify all 21 tables created in `guest_chatbot` schema
3. Check table structures match expected schema

### Phase 2 — Data Copy (no downtime, dev only)
4. Run `scripts/migrate_mysql_to_postgres.py --env dev`
5. Verify row counts match MySQL vs PostgreSQL for each table
6. Run `scripts/migrate_files_to_postgres.py --env dev`
7. Verify file data is in DB

### Phase 3 — Code Changes (dev only)
8. Update `requirements.txt` — add `asyncpg`, remove `aiomysql`
9. Update `models/database.py` — remove MySQL-specific code, add `schema="guest_chatbot"` to all models
10. Update `services/widget_deployment_service.py` — read/write DB instead of JSON file
11. Update `core/context_manager.py` — read/write DB instead of JSON file
12. Update `services/ticketing_service.py` — use DB for local fallback
13. Update `services/rag_service.py` — use DB instead of local index file
14. Update `.env.dev` — point `DATABASE_URL` to PostgreSQL

### Phase 4 — Test on Dev
15. Rebuild Docker with dev PostgreSQL
16. Test all features: admin panel, chat, scraper, images, widget, ticketing
17. Fix any issues

### Phase 5 — Production
18. Get prod PostgreSQL credentials from developer
19. Run same scripts with `--env prod`
20. Schedule brief maintenance window
21. Update `.env` on server → point to prod PostgreSQL
22. Rebuild Docker
23. Keep MySQL running for 1 week as fallback

---

## MySQL → PostgreSQL Type Conversion Reference

| MySQL Type | PostgreSQL Type | Notes |
|---|---|---|
| `INT AUTO_INCREMENT` | `SERIAL` | Primary keys |
| `BIGINT AUTO_INCREMENT` | `BIGSERIAL` | Large tables |
| `VARCHAR(n)` | `VARCHAR(n)` | Same |
| `TEXT` | `TEXT` | Same |
| `LONGTEXT` | `TEXT` | PostgreSQL TEXT has no size limit |
| `TINYINT(1)` | `BOOLEAN` | MySQL booleans |
| `DECIMAL(10,2)` | `NUMERIC(10,2)` | Money fields |
| `JSON` | `JSONB` | JSONB is faster in PostgreSQL |
| `DATETIME` | `TIMESTAMPTZ` | With timezone |
| `DATE` | `DATE` | Same |
| `TIME` | `TIME` | Same |
| `FLOAT` | `DOUBLE PRECISION` | |

---

## What You Need From Developer

1. **Prod PostgreSQL credentials** — IP, port, database, username, password
2. **Confirmation** — is VPN required for the new PostgreSQL server too, or direct access?
3. **Approval** — confirm ok to run migration scripts on prod MySQL (read-only, no changes to MySQL)

---

## Summary of All New Tables Being Created

| Table | Source | Type |
|---|---|---|
| 17 existing tables | MySQL copy | Data migration |
| `widget_deployments` | `config/widget_deployments.json` | New table |
| `local_tickets` | `data/ticketing/local_tickets.json` | New table |
| `conversation_contexts` | `data/runtime/local_contexts.json` | New table |
| `rag_chunks` | `data/rag/local_index.json` | New table |

**Total: 21 tables in `guest_chatbot` schema**
