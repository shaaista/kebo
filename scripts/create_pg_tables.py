"""
Script 1: Create all tables in PostgreSQL guest_chatbot schema.
Run this FIRST before copy_data.py.

Usage:
    python scripts/create_pg_tables.py
"""

import os
import sys
from pathlib import Path

# Load .env from project root
def load_env():
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        print(f"ERROR: .env file not found at {env_path}")
        sys.exit(1)
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)

load_env()

try:
    import psycopg2
except ImportError:
    print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary")
    sys.exit(1)

PG_HOST     = os.environ.get("PG_HOST", "35.154.99.170")
PG_PORT     = int(os.environ.get("PG_PORT", "5433"))
PG_DATABASE = os.environ.get("PG_DATABASE", "bsp_platform")
PG_USER     = os.environ.get("PG_USER", "nexoria")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "nexoria123")
PG_SCHEMA   = os.environ.get("PG_SCHEMA", "guest_chatbot")

TABLES = [
    # ── 1. hotels (no dependencies) ─────────────────────────────────────────
    f"""
    CREATE TABLE IF NOT EXISTS {PG_SCHEMA}.new_bot_hotels (
        id          SERIAL PRIMARY KEY,
        code        VARCHAR(50)  NOT NULL,
        name        VARCHAR(255) NOT NULL,
        city        VARCHAR(100) NOT NULL,
        timezone    VARCHAR(50)  DEFAULT 'Asia/Kolkata',
        is_active   BOOLEAN      DEFAULT TRUE,
        created_at  TIMESTAMPTZ  DEFAULT NOW(),
        updated_at  TIMESTAMPTZ  DEFAULT NOW(),
        CONSTRAINT uq_hotels_code UNIQUE (code)
    )
    """,

    # ── 2. restaurants ───────────────────────────────────────────────────────
    f"""
    CREATE TABLE IF NOT EXISTS {PG_SCHEMA}.new_bot_restaurants (
        id              SERIAL PRIMARY KEY,
        hotel_id        INTEGER      NOT NULL REFERENCES {PG_SCHEMA}.new_bot_hotels(id) ON DELETE CASCADE,
        code            VARCHAR(50)  NOT NULL,
        name            VARCHAR(255) NOT NULL,
        cuisine         VARCHAR(100),
        opens_at        TIME,
        closes_at       TIME,
        delivers_to_room BOOLEAN     DEFAULT FALSE,
        is_active       BOOLEAN      DEFAULT TRUE,
        created_at      TIMESTAMPTZ  DEFAULT NOW(),
        updated_at      TIMESTAMPTZ  DEFAULT NOW(),
        CONSTRAINT uq_rest UNIQUE (hotel_id, code)
    )
    """,

    # ── 3. menu_items ────────────────────────────────────────────────────────
    f"""
    CREATE TABLE IF NOT EXISTS {PG_SCHEMA}.new_bot_menu_items (
        id              SERIAL PRIMARY KEY,
        restaurant_id   INTEGER       NOT NULL REFERENCES {PG_SCHEMA}.new_bot_restaurants(id) ON DELETE CASCADE,
        name            VARCHAR(255)  NOT NULL,
        description     TEXT,
        price           NUMERIC(10,2) DEFAULT 0.00,
        currency        VARCHAR(3)    DEFAULT 'INR',
        category        VARCHAR(100),
        is_vegetarian   BOOLEAN       DEFAULT FALSE,
        is_available    BOOLEAN       DEFAULT TRUE,
        created_at      TIMESTAMPTZ   DEFAULT NOW(),
        updated_at      TIMESTAMPTZ   DEFAULT NOW()
    )
    """,

    # ── 4. guests ────────────────────────────────────────────────────────────
    f"""
    CREATE TABLE IF NOT EXISTS {PG_SCHEMA}.new_bot_guests (
        id              SERIAL PRIMARY KEY,
        hotel_id        INTEGER      NOT NULL REFERENCES {PG_SCHEMA}.new_bot_hotels(id) ON DELETE CASCADE,
        phone_number    VARCHAR(30)  NOT NULL,
        name            VARCHAR(255),
        room_number     VARCHAR(20),
        check_in_date   DATE,
        check_out_date  DATE,
        created_at      TIMESTAMPTZ  DEFAULT NOW(),
        updated_at      TIMESTAMPTZ  DEFAULT NOW(),
        CONSTRAINT uq_guest UNIQUE (hotel_id, phone_number)
    )
    """,

    # ── 5. bookings ──────────────────────────────────────────────────────────
    f"""
    CREATE TABLE IF NOT EXISTS {PG_SCHEMA}.new_bot_bookings (
        id                  SERIAL PRIMARY KEY,
        hotel_id            INTEGER      NOT NULL REFERENCES {PG_SCHEMA}.new_bot_hotels(id) ON DELETE CASCADE,
        guest_id            INTEGER      NOT NULL REFERENCES {PG_SCHEMA}.new_bot_guests(id) ON DELETE CASCADE,
        confirmation_code   VARCHAR(30)  NOT NULL,
        property_name       VARCHAR(255),
        room_number         VARCHAR(20),
        room_type           VARCHAR(100),
        check_in_date       DATE         NOT NULL,
        check_out_date      DATE         NOT NULL,
        num_guests          INTEGER      DEFAULT 1,
        status              VARCHAR(20)  DEFAULT 'reserved',
        source_channel      VARCHAR(20),
        special_requests    TEXT,
        created_at          TIMESTAMPTZ  DEFAULT NOW(),
        updated_at          TIMESTAMPTZ  DEFAULT NOW(),
        CONSTRAINT uq_booking_code UNIQUE (confirmation_code)
    )
    """,

    # ── 6. orders ────────────────────────────────────────────────────────────
    f"""
    CREATE TABLE IF NOT EXISTS {PG_SCHEMA}.new_bot_orders (
        id                  SERIAL PRIMARY KEY,
        guest_id            INTEGER       NOT NULL REFERENCES {PG_SCHEMA}.new_bot_guests(id),
        restaurant_id       INTEGER       NOT NULL REFERENCES {PG_SCHEMA}.new_bot_restaurants(id),
        status              VARCHAR(50)   DEFAULT 'pending',
        total_amount        NUMERIC(10,2) DEFAULT 0.00,
        delivery_location   VARCHAR(100),
        created_at          TIMESTAMPTZ   DEFAULT NOW(),
        updated_at          TIMESTAMPTZ   DEFAULT NOW()
    )
    """,

    # ── 7. order_items ───────────────────────────────────────────────────────
    f"""
    CREATE TABLE IF NOT EXISTS {PG_SCHEMA}.new_bot_order_items (
        id              SERIAL PRIMARY KEY,
        order_id        INTEGER       NOT NULL REFERENCES {PG_SCHEMA}.new_bot_orders(id) ON DELETE CASCADE,
        menu_item_id    INTEGER       NOT NULL REFERENCES {PG_SCHEMA}.new_bot_menu_items(id),
        quantity        INTEGER       DEFAULT 1,
        unit_price      NUMERIC(10,2) DEFAULT 0.00
    )
    """,

    # ── 8. conversations ─────────────────────────────────────────────────────
    f"""
    CREATE TABLE IF NOT EXISTS {PG_SCHEMA}.new_bot_conversations (
        id              SERIAL PRIMARY KEY,
        session_id      VARCHAR(100) NOT NULL,
        hotel_id        INTEGER      NOT NULL REFERENCES {PG_SCHEMA}.new_bot_hotels(id) ON DELETE CASCADE,
        guest_id        INTEGER      REFERENCES {PG_SCHEMA}.new_bot_guests(id) ON DELETE SET NULL,
        booking_id      INTEGER      REFERENCES {PG_SCHEMA}.new_bot_bookings(id) ON DELETE SET NULL,
        state           VARCHAR(50)  DEFAULT 'idle',
        pending_action  VARCHAR(100),
        pending_data    JSONB,
        channel         VARCHAR(20)  DEFAULT 'web',
        created_at      TIMESTAMPTZ  DEFAULT NOW(),
        updated_at      TIMESTAMPTZ  DEFAULT NOW(),
        CONSTRAINT uq_session UNIQUE (session_id)
    )
    """,

    # ── 9. messages (table created, no data copied) ──────────────────────────
    f"""
    CREATE TABLE IF NOT EXISTS {PG_SCHEMA}.new_bot_messages (
        id                  SERIAL PRIMARY KEY,
        conversation_id     INTEGER      NOT NULL REFERENCES {PG_SCHEMA}.new_bot_conversations(id) ON DELETE CASCADE,
        role                VARCHAR(20)  NOT NULL,
        content             TEXT         NOT NULL,
        intent              VARCHAR(100),
        confidence          DOUBLE PRECISION,
        channel             VARCHAR(20)  DEFAULT 'web',
        created_at          TIMESTAMPTZ  DEFAULT NOW()
    )
    """,

    # ── 10. business_config ──────────────────────────────────────────────────
    f"""
    CREATE TABLE IF NOT EXISTS {PG_SCHEMA}.new_bot_business_config (
        id              SERIAL PRIMARY KEY,
        hotel_id        INTEGER      NOT NULL REFERENCES {PG_SCHEMA}.new_bot_hotels(id) ON DELETE CASCADE,
        config_key      VARCHAR(100) NOT NULL,
        config_value    TEXT,
        created_at      TIMESTAMPTZ  DEFAULT NOW(),
        updated_at      TIMESTAMPTZ  DEFAULT NOW(),
        CONSTRAINT uq_config UNIQUE (hotel_id, config_key)
    )
    """,

    # ── 11. capabilities ─────────────────────────────────────────────────────
    f"""
    CREATE TABLE IF NOT EXISTS {PG_SCHEMA}.new_bot_capabilities (
        id              SERIAL PRIMARY KEY,
        hotel_id        INTEGER      NOT NULL REFERENCES {PG_SCHEMA}.new_bot_hotels(id) ON DELETE CASCADE,
        capability_id   VARCHAR(100) NOT NULL,
        description     VARCHAR(255),
        enabled         BOOLEAN      DEFAULT TRUE,
        hours           VARCHAR(100),
        created_at      TIMESTAMPTZ  DEFAULT NOW(),
        updated_at      TIMESTAMPTZ  DEFAULT NOW(),
        CONSTRAINT uq_cap UNIQUE (hotel_id, capability_id)
    )
    """,

    # ── 12. intents ──────────────────────────────────────────────────────────
    f"""
    CREATE TABLE IF NOT EXISTS {PG_SCHEMA}.new_bot_intents (
        id          SERIAL PRIMARY KEY,
        hotel_id    INTEGER      NOT NULL REFERENCES {PG_SCHEMA}.new_bot_hotels(id) ON DELETE CASCADE,
        intent_id   VARCHAR(100) NOT NULL,
        label       VARCHAR(100) NOT NULL,
        enabled     BOOLEAN      DEFAULT TRUE,
        created_at  TIMESTAMPTZ  DEFAULT NOW(),
        updated_at  TIMESTAMPTZ  DEFAULT NOW(),
        CONSTRAINT uq_intent UNIQUE (hotel_id, intent_id)
    )
    """,

    # ── 13. services ─────────────────────────────────────────────────────────
    f"""
    CREATE TABLE IF NOT EXISTS {PG_SCHEMA}.new_bot_services (
        id                              SERIAL PRIMARY KEY,
        hotel_id                        INTEGER      NOT NULL REFERENCES {PG_SCHEMA}.new_bot_hotels(id) ON DELETE CASCADE,
        service_id                      VARCHAR(100) NOT NULL,
        name                            VARCHAR(255) NOT NULL,
        type                            VARCHAR(50)  DEFAULT 'service',
        description                     TEXT,
        phase_id                        VARCHAR(50),
        is_active                       BOOLEAN      DEFAULT TRUE,
        is_builtin                      BOOLEAN      DEFAULT FALSE,
        ticketing_enabled               BOOLEAN      DEFAULT TRUE,
        ticketing_mode                  VARCHAR(20),
        ticketing_policy                TEXT,
        form_config                     JSONB,
        service_prompt_pack             JSONB,
        generated_system_prompt         TEXT,
        generated_system_prompt_override BOOLEAN     DEFAULT FALSE,
        created_at                      TIMESTAMPTZ  DEFAULT NOW(),
        updated_at                      TIMESTAMPTZ  DEFAULT NOW(),
        CONSTRAINT uq_bot_service UNIQUE (hotel_id, service_id)
    )
    """,

    # ── 14. prompt_registry ──────────────────────────────────────────────────
    f"""
    CREATE TABLE IF NOT EXISTS {PG_SCHEMA}.new_bot_prompt_registry (
        id                      SERIAL PRIMARY KEY,
        prompt_key              VARCHAR(128) NOT NULL,
        industry                VARCHAR(32),
        hotel_id                INTEGER      REFERENCES {PG_SCHEMA}.new_bot_hotels(id) ON DELETE CASCADE,
        content                 TEXT         NOT NULL,
        variables               JSONB,
        version                 INTEGER      DEFAULT 1,
        description             TEXT,
        updated_by              VARCHAR(64),
        seeded_from_file_hash   VARCHAR(64),
        is_active               BOOLEAN      DEFAULT TRUE,
        created_at              TIMESTAMPTZ  DEFAULT NOW(),
        updated_at              TIMESTAMPTZ  DEFAULT NOW(),
        CONSTRAINT uq_prompt_scope UNIQUE (prompt_key, industry, hotel_id)
    )
    """,

    # ── 15. kb_files ─────────────────────────────────────────────────────────
    f"""
    CREATE TABLE IF NOT EXISTS {PG_SCHEMA}.new_bot_kb_files (
        id              SERIAL PRIMARY KEY,
        hotel_id        INTEGER      NOT NULL REFERENCES {PG_SCHEMA}.new_bot_hotels(id) ON DELETE CASCADE,
        original_name   VARCHAR(255) NOT NULL,
        stored_name     VARCHAR(255) NOT NULL,
        content         TEXT         NOT NULL,
        content_hash    VARCHAR(64),
        created_at      TIMESTAMPTZ  DEFAULT NOW(),
        updated_at      TIMESTAMPTZ  DEFAULT NOW(),
        CONSTRAINT uq_kb_file UNIQUE (hotel_id, stored_name)
    )
    """,

    # ── 16. bot_hotel_images_scraper ─────────────────────────────────────────
    f"""
    CREATE TABLE IF NOT EXISTS {PG_SCHEMA}.bot_hotel_images_scraper (
        id              SERIAL PRIMARY KEY,
        hotel_id        INTEGER      NOT NULL REFERENCES {PG_SCHEMA}.new_bot_hotels(id) ON DELETE CASCADE,
        title           VARCHAR(255) NOT NULL,
        description     TEXT,
        image_url       TEXT         NOT NULL,
        category        VARCHAR(120),
        tags            JSONB,
        source_label    VARCHAR(255),
        is_active       BOOLEAN      DEFAULT TRUE,
        priority        INTEGER      DEFAULT 0,
        created_at      TIMESTAMPTZ  DEFAULT NOW(),
        updated_at      TIMESTAMPTZ  DEFAULT NOW()
    )
    """,

    # ── 17. scrape_jobs (standalone, no FK) ──────────────────────────────────
    f"""
    CREATE TABLE IF NOT EXISTS {PG_SCHEMA}.scrape_jobs (
        id                  VARCHAR(36)  PRIMARY KEY,
        url                 TEXT         NOT NULL,
        status              VARCHAR(30)  DEFAULT 'pending',
        progress_pct        INTEGER      DEFAULT 0,
        progress_msg        VARCHAR(500) DEFAULT 'Queued...',
        pages_found         INTEGER      DEFAULT 0,
        pages_crawled       INTEGER      DEFAULT 0,
        pages_failed        INTEGER      DEFAULT 0,
        properties_found    INTEGER      DEFAULT 0,
        properties_data     TEXT         DEFAULT '',
        review_data         TEXT         DEFAULT '',
        job_context_data    TEXT         DEFAULT '',
        queue_state         VARCHAR(20)  DEFAULT 'idle',
        task_type           VARCHAR(50)  DEFAULT '',
        task_payload        TEXT         DEFAULT '',
        queue_attempts      INTEGER      DEFAULT 0,
        max_attempts        INTEGER      DEFAULT 1,
        next_retry_at       TIMESTAMPTZ,
        worker_id           VARCHAR(100) DEFAULT '',
        worker_started_at   TIMESTAMPTZ,
        worker_heartbeat_at TIMESTAMPTZ,
        output_dir          TEXT         DEFAULT '',
        kb_preview          TEXT         DEFAULT '',
        error_message       TEXT         DEFAULT '',
        created_at          TIMESTAMPTZ  DEFAULT NOW(),
        completed_at        TIMESTAMPTZ
    )
    """,
]

INDEXES = [
    f"CREATE INDEX IF NOT EXISTS idx_restaurants_hotel_id   ON {PG_SCHEMA}.new_bot_restaurants(hotel_id)",
    f"CREATE INDEX IF NOT EXISTS idx_menu_items_rest_id      ON {PG_SCHEMA}.new_bot_menu_items(restaurant_id)",
    f"CREATE INDEX IF NOT EXISTS idx_guests_hotel_id         ON {PG_SCHEMA}.new_bot_guests(hotel_id)",
    f"CREATE INDEX IF NOT EXISTS idx_bookings_hotel_id       ON {PG_SCHEMA}.new_bot_bookings(hotel_id)",
    f"CREATE INDEX IF NOT EXISTS idx_bookings_guest_id       ON {PG_SCHEMA}.new_bot_bookings(guest_id)",
    f"CREATE INDEX IF NOT EXISTS idx_conversations_hotel_id  ON {PG_SCHEMA}.new_bot_conversations(hotel_id)",
    f"CREATE INDEX IF NOT EXISTS idx_messages_conv_id        ON {PG_SCHEMA}.new_bot_messages(conversation_id)",
    f"CREATE INDEX IF NOT EXISTS idx_business_config_hotel   ON {PG_SCHEMA}.new_bot_business_config(hotel_id)",
    f"CREATE INDEX IF NOT EXISTS idx_capabilities_hotel      ON {PG_SCHEMA}.new_bot_capabilities(hotel_id)",
    f"CREATE INDEX IF NOT EXISTS idx_intents_hotel           ON {PG_SCHEMA}.new_bot_intents(hotel_id)",
    f"CREATE INDEX IF NOT EXISTS idx_services_hotel          ON {PG_SCHEMA}.new_bot_services(hotel_id)",
    f"CREATE INDEX IF NOT EXISTS idx_prompt_registry_key     ON {PG_SCHEMA}.new_bot_prompt_registry(prompt_key)",
    f"CREATE INDEX IF NOT EXISTS idx_kb_files_hotel          ON {PG_SCHEMA}.new_bot_kb_files(hotel_id)",
    f"CREATE INDEX IF NOT EXISTS idx_images_hotel            ON {PG_SCHEMA}.bot_hotel_images_scraper(hotel_id)",
]

TABLE_NAMES = [
    "new_bot_hotels", "new_bot_restaurants", "new_bot_menu_items",
    "new_bot_guests", "new_bot_bookings", "new_bot_orders",
    "new_bot_order_items", "new_bot_conversations", "new_bot_messages",
    "new_bot_business_config", "new_bot_capabilities", "new_bot_intents",
    "new_bot_services", "new_bot_prompt_registry", "new_bot_kb_files",
    "bot_hotel_images_scraper", "scrape_jobs",
]


def main():
    print("=" * 60)
    print("CREATE POSTGRESQL TABLES")
    print(f"Host:     {PG_HOST}:{PG_PORT}")
    print(f"Database: {PG_DATABASE}")
    print(f"Schema:   {PG_SCHEMA}")
    print("=" * 60)

    try:
        conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT, dbname=PG_DATABASE,
            user=PG_USER, password=PG_PASSWORD,
            connect_timeout=15,
        )
        conn.autocommit = True
        cur = conn.cursor()
        print("Connected to PostgreSQL ✓\n")
    except Exception as e:
        print(f"ERROR: Cannot connect to PostgreSQL — {e}")
        print("Check your VPN is ON and credentials are correct.")
        sys.exit(1)

    # Create schema if not exists (requires CREATE privilege on database)
    try:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {PG_SCHEMA}")
        print(f"Schema '{PG_SCHEMA}' created/verified ✓\n")
    except Exception as e:
        # Schema may already exist or user lacks CREATE privilege — try proceeding anyway
        conn.rollback()
        print(f"Schema create skipped ({e})")
        print(f"Assuming schema '{PG_SCHEMA}' already exists — proceeding...\n")

    # Create tables
    for i, (name, ddl) in enumerate(zip(TABLE_NAMES, TABLES), 1):
        try:
            cur.execute(ddl)
            print(f"  [{i:02d}/17] {name} ✓")
        except Exception as e:
            print(f"  [{i:02d}/17] {name} ERROR: {e}")
            conn.close()
            sys.exit(1)

    print("\nCreating indexes...")
    for idx_sql in INDEXES:
        try:
            cur.execute(idx_sql)
        except Exception as e:
            print(f"  Index warning (non-fatal): {e}")

    cur.close()
    conn.close()

    print("\n" + "=" * 60)
    print("ALL 17 TABLES CREATED SUCCESSFULLY")
    print("Now run: python scripts/copy_data.py")
    print("=" * 60)


if __name__ == "__main__":
    main()
