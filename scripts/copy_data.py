"""
Script 2: Copy data from MySQL → PostgreSQL for 16 tables.
Skips new_bot_messages (table exists but no data is copied by design).

Run AFTER scripts/create_pg_tables.py.

Usage:
    python scripts/copy_data.py
"""

import os
import sys
import json
from pathlib import Path


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
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()

try:
    import pymysql
    import pymysql.cursors
except ImportError:
    print("ERROR: pymysql not installed. Run: pip install pymysql")
    sys.exit(1)

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary")
    sys.exit(1)

# ── Credentials ──────────────────────────────────────────────────────────────
MY_HOST     = os.environ.get("MYSQL_HOST", "172.16.5.32")
MY_PORT     = int(os.environ.get("MYSQL_PORT", "3306"))
MY_USER     = os.environ.get("MYSQL_USER", "root")
MY_PASSWORD = os.environ.get("MYSQL_PASSWORD", "zapcom123")
MY_DATABASE = os.environ.get("MYSQL_DATABASE", "GHN_PROD_BAK")

PG_HOST     = os.environ.get("PG_HOST", "35.154.99.170")
PG_PORT     = int(os.environ.get("PG_PORT", "5433"))
PG_DATABASE = os.environ.get("PG_DATABASE", "bsp_platform")
PG_USER     = os.environ.get("PG_USER", "nexoria")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "nexoria123")
PG_SCHEMA   = os.environ.get("PG_SCHEMA", "guest_chatbot")

BATCH_SIZE = 500

# ── Tables to copy, in FK dependency order ────────────────────────────────────
# new_bot_messages is intentionally excluded (table created, no data copied)
TABLES_TO_COPY = [
    "new_bot_hotels",
    "new_bot_restaurants",
    "new_bot_menu_items",
    "new_bot_guests",
    "new_bot_bookings",
    "new_bot_orders",
    "new_bot_order_items",
    "new_bot_conversations",
    "new_bot_business_config",
    "new_bot_capabilities",
    "new_bot_intents",
    "new_bot_services",
    "new_bot_prompt_registry",
    "new_bot_kb_files",
    "bot_hotel_images_scraper",
    "scrape_jobs",
]

# Columns that hold JSON data — need psycopg2.extras.Json() wrapping for JSONB
JSON_COLUMNS = {
    "new_bot_conversations":    {"pending_data"},
    "new_bot_services":         {"form_config", "service_prompt_pack"},
    "new_bot_prompt_registry":  {"variables"},
    "bot_hotel_images_scraper": {"tags"},
}

# MySQL stores BOOLEAN as TINYINT(1) — must cast to Python bool for PostgreSQL
BOOLEAN_COLUMNS = {
    "new_bot_hotels":           {"is_active"},
    "new_bot_restaurants":      {"delivers_to_room", "is_active"},
    "new_bot_menu_items":       {"is_vegetarian", "is_available"},
    "new_bot_capabilities":     {"enabled"},
    "new_bot_intents":          {"enabled"},
    "new_bot_services":         {"is_active", "is_builtin", "ticketing_enabled", "generated_system_prompt_override"},
    "new_bot_prompt_registry":  {"is_active"},
    "bot_hotel_images_scraper": {"is_active"},
}

# MySQL stores BOOLEAN as TINYINT(1) — must cast to Python bool for PostgreSQL
BOOLEAN_COLUMNS = {
    "new_bot_hotels":        {"is_active"},
    "new_bot_restaurants":   {"delivers_to_room", "is_active"},
    "new_bot_menu_items":    {"is_vegetarian", "is_available"},
    "new_bot_capabilities":  {"enabled"},
    "new_bot_intents":       {"enabled"},
    "new_bot_services":      {"is_active", "is_builtin", "ticketing_enabled", "generated_system_prompt_override"},
    "new_bot_prompt_registry": {"is_active"},
    "bot_hotel_images_scraper": {"is_active"},
}


def fix_value(table: str, col: str, val):
    """Convert a MySQL cell value to a PostgreSQL-safe Python value."""
    if val is None:
        return None

    # Boolean columns: MySQL returns int 0/1, PostgreSQL needs True/False
    if col in BOOLEAN_COLUMNS.get(table, set()):
        return bool(val)

    # JSONB columns: wrap in Json() so psycopg2 serialises correctly
    if col in JSON_COLUMNS.get(table, set()):
        if isinstance(val, (dict, list)):
            return psycopg2.extras.Json(val)
        if isinstance(val, (str, bytes)):
            try:
                return psycopg2.extras.Json(json.loads(val))
            except (json.JSONDecodeError, ValueError):
                return psycopg2.extras.Json(None)
        return psycopg2.extras.Json(val)

    # MySQL may return bytes for TEXT/LONGTEXT columns
    if isinstance(val, (bytes, bytearray)):
        return val.decode("utf-8", errors="replace")

    return val


def copy_table(my_cur, pg_conn, pg_cur, table: str) -> tuple:
    """Copy all rows from one MySQL table into PostgreSQL. Returns (mysql_count, inserted)."""
    my_cur.execute(f"SELECT COUNT(*) AS cnt FROM `{table}`")
    total = (my_cur.fetchone() or {}).get("cnt", 0)

    if total == 0:
        return 0, 0

    # Discover columns at runtime
    my_cur.execute(f"SELECT * FROM `{table}` LIMIT 0")
    columns  = [d[0] for d in my_cur.description]
    col_list = ", ".join(f'"{c}"' for c in columns)

    # execute_values format: INSERT INTO t (cols) VALUES %s ON CONFLICT DO NOTHING
    insert_sql = (
        f'INSERT INTO {PG_SCHEMA}."{table}" ({col_list}) '
        f'VALUES %s ON CONFLICT DO NOTHING'
    )

    inserted = 0
    offset   = 0
    errors   = 0

    while offset < total:
        my_cur.execute(f"SELECT * FROM `{table}` LIMIT {BATCH_SIZE} OFFSET {offset}")
        rows = my_cur.fetchall()
        if not rows:
            break

        batch = [
            tuple(fix_value(table, col, row[col]) for col in columns)
            for row in rows
        ]

        try:
            psycopg2.extras.execute_values(pg_cur, insert_sql, batch, page_size=BATCH_SIZE)
            pg_conn.commit()
            inserted += pg_cur.rowcount  # actual rows inserted (ON CONFLICT skips = not counted)
        except Exception as e:
            pg_conn.rollback()
            print(f"\n    Batch error at offset {offset}: {e}")
            # Row-by-row fallback
            placeholders = ", ".join(["%s"] * len(columns))
            row_sql = (
                f'INSERT INTO {PG_SCHEMA}."{table}" ({col_list}) '
                f'VALUES ({placeholders}) ON CONFLICT DO NOTHING'
            )
            for fixed_row in batch:
                try:
                    pg_cur.execute(row_sql, fixed_row)
                    pg_conn.commit()
                    inserted += pg_cur.rowcount
                except Exception as row_err:
                    pg_conn.rollback()
                    errors += 1
                    if errors <= 3:
                        print(f"\n    Row error: {row_err} | values: {str(fixed_row)[:120]}")

        offset += BATCH_SIZE
        pct = min(100, int(offset / total * 100))
        print(f"\r    {min(offset, total)}/{total} rows ({pct}%)", end="", flush=True)

    print()
    return total, inserted


def reset_sequence(pg_cur, pg_conn, table: str, pk: str = "id"):
    """Advance the SERIAL sequence past the max id so future inserts don't conflict."""
    seq = f"{PG_SCHEMA}.{table}_{pk}_seq"
    try:
        pg_cur.execute(
            f'SELECT setval(\'{seq}\', '
            f'COALESCE((SELECT MAX("{pk}") FROM {PG_SCHEMA}."{table}"), 1))'
        )
        pg_conn.commit()
    except Exception:
        pg_conn.rollback()  # scrape_jobs uses VARCHAR pk — no sequence, safe to ignore


def main():
    print("=" * 60)
    print("COPY DATA: MySQL → PostgreSQL")
    print(f"MySQL:     {MY_HOST}:{MY_PORT}/{MY_DATABASE}")
    print(f"Postgres:  {PG_HOST}:{PG_PORT}/{PG_DATABASE} / schema={PG_SCHEMA}")
    print(f"Tables:    {len(TABLES_TO_COPY)} tables")
    print(f"Skipping:  new_bot_messages data (table exists, no data)")
    print("=" * 60)

    # ── Connect MySQL ──────────────────────────────────────────────────────────
    try:
        my_conn = pymysql.connect(
            host=MY_HOST, port=MY_PORT,
            user=MY_USER, password=MY_PASSWORD,
            database=MY_DATABASE,
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=15,
            charset="utf8mb4",
        )
        my_cur = my_conn.cursor()
        print("Connected to MySQL ✓")
    except Exception as e:
        print(f"ERROR: Cannot connect to MySQL — {e}")
        print("Is your VPN on? Check MYSQL_* variables in .env")
        sys.exit(1)

    # ── Connect PostgreSQL ─────────────────────────────────────────────────────
    try:
        pg_conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT, dbname=PG_DATABASE,
            user=PG_USER, password=PG_PASSWORD,
            connect_timeout=15,
        )
        pg_cur = pg_conn.cursor()
        print("Connected to PostgreSQL ✓\n")
    except Exception as e:
        print(f"ERROR: Cannot connect to PostgreSQL — {e}")
        print("Is your VPN on? Check PG_* variables in .env")
        my_conn.close()
        sys.exit(1)

    # ── Copy each table ────────────────────────────────────────────────────────
    results = []

    for i, table in enumerate(TABLES_TO_COPY, 1):
        label = f"[{i:02d}/{len(TABLES_TO_COPY)}]"
        print(f"{label} {table}")
        try:
            mysql_count, pg_inserted = copy_table(my_cur, pg_conn, pg_cur, table)
            reset_sequence(pg_cur, pg_conn, table)
            results.append((table, mysql_count, pg_inserted, None))
            print(f"    Done — {pg_inserted} inserted  (MySQL: {mysql_count})\n")
        except Exception as e:
            pg_conn.rollback()
            results.append((table, 0, 0, str(e)))
            print(f"    FAILED — {e}\n")

    my_cur.close()
    my_conn.close()
    pg_cur.close()
    pg_conn.close()

    # ── Summary ────────────────────────────────────────────────────────────────
    print("=" * 60)
    print("MIGRATION SUMMARY")
    print("=" * 60)
    print(f"{'Table':<38} {'MySQL':>7} {'Inserted':>9}  Status")
    print("-" * 62)
    total_mysql = total_inserted = 0
    errors = []
    for table, mc, pi, err in results:
        status = "OK" if err is None else "ERROR"
        print(f"{table:<38} {mc:>7} {pi:>9}  {status}")
        if err:
            errors.append(f"  {table}: {err}")
        total_mysql    += mc
        total_inserted += pi
    print("-" * 62)
    print(f"{'TOTAL':<38} {total_mysql:>7} {total_inserted:>9}")
    print()
    if errors:
        print("Errors:")
        for e in errors:
            print(e)
        print()
    print("new_bot_messages  — table created, no data copied (by design)")
    print("=" * 60)
    if not errors:
        print("Migration complete. Run create_pg_tables.py first if you haven't.")


if __name__ == "__main__":
    main()
